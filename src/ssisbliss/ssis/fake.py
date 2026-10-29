"""Synthetic SSIS packages, .ispac files and a fake SSIS catalog.

Used by the unit tests and by the hill climb. The CLI reaches it with
`--server fake://<directory>`: state lives in <directory>/state.json plus one .ispac
per stored project version, and every change is written back immediately. The caller's
identity comes from SSISBLISS_FAKE_USER (default LAB\\me).
"""
from __future__ import annotations

import copy
import io
import json
import os
import re
import sys
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote

from ssisbliss.ssis.catalog import Project, ServerInfo
from ssisbliss.ssis.ispac import Ispac

DTS = "www.microsoft.com/SqlServer/Dts"
SSIS = "www.microsoft.com/SqlServer/SSIS"


def _clock(tick: float) -> str:
    """A version's time in the fake: one hour per lsn, so created and restored times order like SSISDB's."""
    from datetime import datetime, timedelta
    return (datetime(2026, 10, 1) + timedelta(hours=tick)).isoformat(timespec="seconds")


def fake_location(server: str) -> Path:
    """`fake://<path>`, or `fake://<name>` under $SSISBLISS_FAKE_ROOT (the climb keeps its servers' location out of
    ssisbliss.toml, where a real estate has a server name and nothing to browse)."""
    spec = server.removeprefix("fake://")
    root = os.environ.get("SSISBLISS_FAKE_ROOT")
    return Path(root) / spec if root and "/" not in spec and "\\" not in spec else Path(spec)


def stored_like_ssisdb(data: bytes) -> bytes:
    """What SSISDB keeps for a deployed .ispac: every package re-serialized with ProtectionLevel
    ServerStorage, a new VersionGUID and its root attributes in a different order (seen on 15.0.4470)."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            part = zin.read(item.filename)
            if item.filename.lower().endswith(".dtsx"):
                part = _restored_package(part)
            elif item.filename == "@Project.manifest":
                part = re.sub(rb'SSIS:ProtectionLevel="[^"]*"', b'SSIS:ProtectionLevel="ServerStorage"', part, count=1)
            zout.writestr(item, part)
    return out.getvalue()


def _restored_package(data: bytes) -> bytes:
    """Like ISServerExec re-saving a package with the 15.0 runtime (see dtsx.package_sha256)."""
    start = data.index(b"<DTS:Executable")
    end = data.index(b">", start)
    tag = re.sub(rb'\s+DTS:(?:ProtectionLevel|VersionGUID|LastModifiedProductVersion)="[^"]*"', b"", data[start:end])
    guid = str(uuid.uuid4()).upper().encode()
    tag = tag.replace(b"<DTS:Executable", b'<DTS:Executable DTS:ProtectionLevel="5"', 1)
    data = (data[:start] + tag + b'\n  DTS:LastModifiedProductVersion="15.0.4470.1"\n  DTS:VersionGUID="{' + guid
            + b'}"' + data[end:])
    data = re.sub(rb'\s+DTS:(?:ConnectRetryCount|ConnectRetryInterval|ConnectByProxy)="[^"]*"', b"", data)
    if b'DTS:PropertyExpression\n        DTS:Name="ConnectionString"' in data or \
            b'DTS:PropertyExpression DTS:Name="ConnectionString"' in data:
        # Expression-driven connection strings are recalculated with the deployed parameter values.
        data = re.sub(rb'(<DTS:ObjectData>\s*<DTS:ConnectionManager\s+DTS:ConnectionString=")([^"]*)(")',
                      rb"\1\2;Application Name=evaluated on the server\3", data)
    return data


def dtsx(name: str, body: str = "", protection: int | None = 0, fmt: int = 8, assembly_major: int = 15,
         sensitive: bool = False, revision: int = 1) -> bytes:
    prot = f' DTS:ProtectionLevel="{protection}"' if protection is not None else ""
    secret = ('<DTS:PackageParameters><DTS:PackageParameter DTS:ObjectName="Pwd" DTS:Sensitive="1">'
              '<DTS:Property DTS:Name="ParameterValue" DTS:Sensitive="1">c2VjcmV0</DTS:Property>'
              '</DTS:PackageParameter></DTS:PackageParameters>') if sensitive else ""
    return f"""<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="{DTS}" DTS:refId="Package" DTS:ExecutableType="Microsoft.Package"
  DTS:ObjectName="{name.removesuffix('.dtsx')}" DTS:DTSID="{{{uuid.uuid5(uuid.NAMESPACE_URL, name)}}}"
  DTS:VersionGUID="{{{uuid.uuid4()}}}" DTS:VersionBuild="{revision}"{prot}>
  <DTS:Property DTS:Name="PackageFormatVersion">{fmt}</DTS:Property>
  {secret}
  <DTS:Executables>
    <DTS:Executable DTS:refId="Package\\Data Flow" DTS:ExecutableType="Microsoft.Pipeline">
      <DTS:ObjectData><pipeline version="1"><components><component
        componentClassID="Microsoft.ScriptComponentHost"
        description="Microsoft.SqlServer.Dts.Pipeline.ScriptComponentHost, Microsoft.SqlServer.TxScript, Version={assembly_major}.0.0.0"
      /></components></pipeline></DTS:ObjectData>
    </DTS:Executable>
  </DTS:Executables>
  {body}
</DTS:Executable>
""".encode()


def conmgr(name: str) -> bytes:
    return (f'<?xml version="1.0"?><DTS:ConnectionManager xmlns:DTS="{DTS}" DTS:ObjectName="{name}" '
            f'DTS:DTSID="{{{uuid.uuid5(uuid.NAMESPACE_URL, name)}}}" DTS:CreationName="OLEDB" />').encode()


def params(names: list[str]) -> bytes:
    inner = "".join(f'<SSIS:Parameter SSIS:Name="{n}"><SSIS:Properties>'
                    f'<SSIS:Property SSIS:Name="Sensitive">0</SSIS:Property></SSIS:Properties></SSIS:Parameter>'
                    for n in names)
    return f'<?xml version="1.0"?><SSIS:Parameters xmlns:SSIS="{SSIS}">{inner}</SSIS:Parameters>'.encode()


def ispac(project: str, packages: dict[str, bytes], parameters: list[str] | None = None,
          connection_managers: list[str] | None = None, protection: str = "DontSaveSensitive") -> bytes:
    parameters = parameters or []
    connection_managers = connection_managers or []
    pkgs = "".join(f'<SSIS:Package SSIS:Name="{n}" SSIS:EntryPoint="1" />' for n in packages)
    cms = "".join(f'<SSIS:ConnectionManager SSIS:Name="{c}.conmgr" />' for c in connection_managers)
    meta = "".join(f'<SSIS:PackageMetaData SSIS:Name="{n}"><SSIS:Properties /><SSIS:Parameters /></SSIS:PackageMetaData>'
                   for n in packages)
    # Like SSDT and SSISDB exports: no XML declaration (SSISDB rejects a manifest that has one).
    manifest = (f'<SSIS:Project SSIS:ProtectionLevel="{protection}" xmlns:SSIS="{SSIS}">'
                f'<SSIS:Properties><SSIS:Property SSIS:Name="Name">{project}</SSIS:Property></SSIS:Properties>'
                f'<SSIS:Packages>{pkgs}</SSIS:Packages><SSIS:ConnectionManagers>{cms}</SSIS:ConnectionManagers>'
                f'<SSIS:DeploymentInfo><SSIS:ProjectConnectionParameters /><SSIS:PackageInfo>{meta}'
                f'</SSIS:PackageInfo></SSIS:DeploymentInfo></SSIS:Project>')
    content_types = ('<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="dtsx" ContentType="text/xml" /><Default Extension="conmgr" ContentType="text/xml" />'
                     '<Default Extension="params" ContentType="text/xml" /><Default Extension="manifest" ContentType="text/xml" />'
                     '</Types>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("@Project.manifest", manifest)
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("Project.params", params(parameters))
        for c in connection_managers:
            z.writestr(quote(f"{c}.conmgr"), conmgr(c))
        for n, data in packages.items():
            z.writestr(quote(n), data)
    return out.getvalue()


@dataclass
class FakeProject:
    folder: str
    name: str
    ispac: bytes
    project_id: int
    lsn: int
    deployed_by: str = "LAB\\alice"
    bindings: list[dict] = field(default_factory=list)
    env_refs: list[dict] = field(default_factory=list)
    history: dict[int, bytes] = field(default_factory=dict)
    created_by: dict = field(default_factory=dict)   # lsn -> who deployed that version (catalog.object_versions)
    restored: dict = field(default_factory=dict)     # lsn -> clock (next_lsn + 0.5) when last restored


class FakeCatalog:
    """In-memory stand-in for SSISDB with the semantics the deploy logic relies on."""

    def __init__(self, server_name: str = "LABSQL\\SQLDEV", major: int = 15, user: str = "LAB\\me"):
        self.info = ServerInfo(server_name, major, f"{major}.0.4470.1", True, True)
        self.user = user
        self.projects: dict[tuple[str, str], FakeProject] = {}
        self.folders: set[str] = set()
        self.running = 0
        self.next_lsn = 100
        self.calls: list[str] = []
        self.scheduled: list[dict] = []
        self.read_only_users: set[str] = set()
        self.execution_rows: list[dict] = []
        self.event_rows: dict[int, list[dict]] = {}
        self.failed_rows: dict[int, list[dict]] = {}
        self.parameter_rows: dict[int, list[dict]] = {}   # users with READ but no MODIFY (setup only, not persisted)
        self.path: Path | None = None

    # --- persistence
    @classmethod
    def load(cls, path: str | Path) -> "FakeCatalog":
        path = Path(path)
        state = json.loads((path / "state.json").read_text())
        with open(path / "access.log", "a") as f:   # lets graders see the server was reached
            f.write("load\n")
        c = cls(state["server_name"], state["major"], os.environ.get("SSISBLISS_FAKE_USER", "LAB\\me"))
        c.running, c.next_lsn, c.folders = state["running"], state["next_lsn"], set(state["folders"])
        c.calls = state.get("calls", [])
        for p in state["projects"]:
            history = {int(lsn): (path / "versions" / f"{p['project_id']}-{lsn}.ispac").read_bytes()
                       for lsn in p.pop("history")}
            p["created_by"] = {int(k): v for k, v in p.get("created_by", {}).items()}
            p["restored"] = {int(k): v for k, v in p.get("restored", {}).items()}
            fp = FakeProject(**p, ispac=history[p["lsn"]], history=history)
            c.projects[(fp.folder, fp.name)] = fp
        c.path = path
        c.scheduled = state.get("scheduled", [])
        ex = state.get("executions", {})
        c.execution_rows = ex.get("rows", [])
        c.event_rows, c.failed_rows, c.parameter_rows = (
            {int(k): v for k, v in ex.get(part, {}).items()} for part in ("events", "failed", "parameters"))
        if "--apply" in sys.argv and c.scheduled:
            c._run_scheduled()
        return c

    def _run_scheduled(self) -> None:
        """Someone else deploys between the caller's plan and apply (climb race scenarios)."""
        events, self.scheduled = self.scheduled, []
        for e in events:
            saved, self.user = self.user, e["who"]
            self.deploy_packages(e["folder"], e["project"],
                                 [(e["package"], (self.path / e["file"]).read_bytes())])  # type: ignore[operator]
            self.user = saved

    def save(self, path: str | Path | None = None) -> None:
        path = Path(path) if path else self.path
        if path is None:
            return
        self.path = path
        (path / "versions").mkdir(parents=True, exist_ok=True)
        projects = []
        for p in self.projects.values():
            d = asdict(p)
            d.pop("ispac")
            d["history"] = sorted(p.history)
            for lsn, data in p.history.items():
                (path / "versions" / f"{p.project_id}-{lsn}.ispac").write_bytes(data)
            projects.append(d)
        state = {"server_name": self.info.server_name, "major": self.info.major_version, "running": self.running,
                 "next_lsn": self.next_lsn, "folders": sorted(self.folders), "projects": projects,
                 "calls": self.calls, "scheduled": self.scheduled,
                 "executions": {"rows": self.execution_rows, "events": self.event_rows,
                                "failed": self.failed_rows, "parameters": self.parameter_rows}}
        (path / "state.json").write_text(json.dumps(state, indent=2))

    # --- setup helpers
    def add_project(self, folder: str, name: str, data: bytes, **kw) -> FakeProject:
        self.folders.add(folder)
        self.next_lsn += 1
        data = stored_like_ssisdb(data)
        p = FakeProject(folder, name, data, project_id=len(self.projects) + 1, lsn=self.next_lsn, **kw)
        p.history[p.lsn] = data
        p.created_by[p.lsn] = p.deployed_by
        self.projects[(folder, name)] = p
        self.save()
        return p

    def someone_else_deploys(self, folder: str, project: str, name: str, data: bytes, who: str = "LAB\\bob"):
        saved, self.user = self.user, who
        self.deploy_packages(folder, project, [(name, data)])
        self.user = saved

    def _p(self, folder: str, project: str) -> FakeProject:
        return self.projects[(folder, project)]

    def _new_version(self, p: FakeProject, data: bytes) -> None:
        self.next_lsn += 1
        data = stored_like_ssisdb(data)
        p.ispac, p.lsn, p.deployed_by = data, self.next_lsn, self.user
        p.history[p.lsn] = data
        p.created_by[p.lsn] = self.user
        # Like SSISDB: bindings for parameters that no longer exist are dropped.
        names = Ispac(data).parameter_names()
        p.bindings = [b for b in p.bindings if b["object_type"] != 20 or b["parameter_name"] in names]
        self.save()

    # --- Catalog interface
    def server_info(self) -> ServerInfo:
        return self.info

    def login(self) -> str:
        return self.user

    def folder_exists(self, folder: str) -> bool:
        return folder in self.folders

    def project(self, folder: str, project: str) -> Project | None:
        p = self.projects.get((folder, project))
        if p is None:
            return None
        return Project(p.project_id, folder, project, p.lsn, p.deployed_by, f"2026-10-{p.lsn % 28 + 1:02d}T10:00:00")

    def _by_id(self, project_id: int) -> FakeProject:
        return next(p for p in self.projects.values() if p.project_id == project_id)

    def can_modify_project(self, project_id: int) -> bool:
        return self.user not in self.read_only_users

    def packages(self, project_id: int) -> list[str]:
        return sorted(Ispac(self._by_id(project_id).ispac).package_names)

    def project_parameters(self, project_id: int) -> list[str]:
        return sorted(Ispac(self._by_id(project_id).ispac).parameter_names())

    def parameter_bindings(self, project_id: int) -> list[dict]:
        return copy.deepcopy(self._by_id(project_id).bindings)

    def environment_references(self, project_id: int) -> list[dict]:
        return copy.deepcopy(self._by_id(project_id).env_refs)

    def running_executions(self, folder: str, project: str) -> int:
        return self.running

    def versions(self, project_id: int) -> list[dict]:
        p = self._by_id(project_id)
        return [{"object_version_lsn": lsn, "created_by": p.created_by.get(lsn, p.deployed_by),
                 "created_time": _clock(lsn), "restored_by": p.deployed_by if lsn in p.restored else None,
                 "last_restored_time": _clock(p.restored[lsn]) if lsn in p.restored else None}
                for lsn in sorted(p.history, reverse=True)]

    def get_project(self, folder: str, project: str) -> bytes:
        return self._p(folder, project).ispac

    def deploy_packages(self, folder: str, project: str, packages: list[tuple[str, bytes]]) -> int:
        self.calls.append("deploy_packages")
        p = self._p(folder, project)
        current = Ispac(p.ispac)
        pkgs = {n: current.package(n) for n in current.package_names}
        pkgs.update(dict(packages))
        source = Ispac(ispac(project, pkgs))
        self._new_version(p, current.merged(source, [n for n, _ in packages], project_items=False))
        return 1

    def deploy_project(self, folder: str, project: str, data: bytes) -> int:
        self.calls.append("deploy_project")
        manifest = Ispac(data).parts["@Project.manifest"][1].lstrip(b"\xef\xbb\xbf").lstrip()
        if not manifest.startswith(b"<SSIS:Project"):
            # Like SSISDB 15.0, which rejects even a leading <?xml ...?> declaration.
            raise RuntimeError("operation failed: 'Project' element for Project is missing in the project manifest.")
        self._new_version(self._p(folder, project), data)
        return 2

    def restore_project(self, folder: str, project: str, lsn: int) -> None:
        self.calls.append("restore_project")
        p = self._p(folder, project)
        p.ispac, p.lsn = p.history[lsn], lsn
        p.restored[lsn] = self.next_lsn + 0.5
        self.save()

    def operation(self, operation_id: int) -> dict:
        return {"operation_id": operation_id, "status": "succeeded", "messages": []}

    # --- executions (set by tests and climb worlds; saved with the rest of the state)
    def executions(self, folder=None, project=None, package=None, statuses=None, since_hours=None, limit=20,
                   execution_id=None) -> list[dict]:
        rows = [r for r in self.execution_rows
                if (folder is None or r["folder_name"] == folder) and (project is None or r["project_name"] == project)
                and (package is None or r["package_name"] == package) and (not statuses or r["status"] in statuses)
                and (execution_id is None or r["execution_id"] == execution_id)]
        return sorted(rows, key=lambda r: -r["execution_id"])[:limit]

    def execution_events(self, execution_id: int, message_types: tuple[int, ...] = (120, 130)) -> list[dict]:
        return [e for e in self.event_rows.get(execution_id, []) if e["message_type"] in message_types]

    def failed_executables(self, execution_id: int) -> list[dict]:
        return self.failed_rows.get(execution_id, [])

    def execution_parameters(self, execution_id: int) -> list[dict]:
        return self.parameter_rows.get(execution_id, [])

    def list_projects(self, folder: str | None) -> list[dict]:
        return [{"folder": p.folder, "project": p.name} for p in self.projects.values()
                if folder in (None, p.folder)]
