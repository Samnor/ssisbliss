"""`jobs --check`: job steps that will fail on their next run, checked against the SSIS catalog. Read-only.

A deploy can rename or remove a package, drop a parameter or an environment reference while the SQL
Agent job that runs it keeps the old names: the job then fails at night, without an execution to look
at. Each problem names the step, what it refers to that is not there, and the closest names that are.
"""
from __future__ import annotations

import difflib
import xml.etree.ElementTree as ET

from ..ssis.dtsx import DTS
from ..ssis.ispac import Ispac
from .status import parse_ssis_command

_LOCAL = {".", "(local)", "localhost", "127.0.0.1"}


def _host_instance(name: str, local_host: str) -> str:
    host, _, instance = name.strip().strip('"\\').partition("\\")
    host = host.split(",")[0]
    if host.lower() in _LOCAL:
        host = local_host
    return f"{host}\\{instance}".lower() if instance else host.lower()


def same_server(step_server: str | None, server_name: str) -> bool:
    """A step's /SERVER is this server: its name, or a local alias (., localhost) for the same instance."""
    if not step_server:
        return True
    return _host_instance(step_server, server_name.split("\\")[0]) == server_name.lower()


def _package_names(data: bytes) -> tuple[set[str], set[str]]:
    """A package's own parameters and connection managers."""
    root = ET.fromstring(data)
    q = lambda tag: f"{{{DTS}}}{tag}"  # noqa: E731
    params = {p.get(q("ObjectName"), "") for p in root.iter(q("PackageParameter"))}
    cms = root.find(q("ConnectionManagers"))
    conns = {c.get(q("ObjectName"), "") for c in (cms if cms is not None else []) if c.tag == q("ConnectionManager")}
    return params, conns


def _close(name: str, names) -> list[str]:
    return difflib.get_close_matches(name, sorted(names), n=3, cutoff=0.5)


class _Project:
    """What a deployed project has, read once per project."""

    def __init__(self, catalog, folder: str, project: str):
        self.found = catalog.project(folder, project)
        self.folder_found = self.found is not None or catalog.folder_exists(folder)
        self.ispac: Ispac | None = None
        self.env_refs: list[dict] = []
        self.packages: list[str] = []
        if self.found is None:
            return
        self.env_refs = catalog.environment_references(self.found.project_id)
        try:
            self.ispac = Ispac(catalog.get_project(folder, project))
            self.packages = list(self.ispac.package_names)
        except Exception:   # noqa: BLE001 - no READ on the project's contents: check names from the catalog only
            self.packages = catalog.packages(self.found.project_id)

    def package(self, name: str) -> str | None:
        """The deployed package of that name (catalog names ignore case)."""
        return next((p for p in self.packages if p.lower() == name.lower()), None)


def _parameter_problem(name: str, project: _Project, package: str) -> str | None:
    """Why `/Par name` does not exist in the deployed project, or None."""
    if project.ispac is None:
        return None
    scope, _, rest = name.partition("::")
    project_params = project.ispac.parameter_names()
    project_cms = project.ispac.connection_manager_names()
    pkg_params, pkg_cms = _package_names(project.ispac.package(package))
    if rest.startswith("CM."):
        cm = rest[3:].rsplit(".", 1)[0]
        known = project_cms if scope == "$Project" else pkg_cms | project_cms
        if cm in known:
            return None
        where = "the project" if scope == "$Project" else f"{package} or the project"
        close = _close(cm, known)
        return f"{where} has no connection manager '{cm}'" + (f" (did you mean: {', '.join(close)})" if close else "")
    known, where = (project_params, "the project") if scope == "$Project" else (pkg_params, package)
    if rest in known:
        return None
    close = _close(rest, known)
    return f"{where} has no parameter '{rest}'" + (f" (did you mean: {', '.join(close)})" if close else "")


def step_problems(steps: list[dict], catalog, server_name: str) -> dict[str, list[dict]]:
    """Problems by job name: catalog steps naming a package, folder, parameter or environment reference
    that is not deployed, and steps that jump to a step that does not exist."""
    projects: dict[tuple[str, str], _Project] = {}
    out: dict[str, list[dict]] = {}
    ids: dict[str, set[int]] = {}
    for s in steps:
        ids.setdefault(s["job"], set()).add(s["step_id"])

    def add(s: dict, problem: str, fix: str, severity: str = "error") -> None:
        out.setdefault(s["job"], []).append({"step_id": s["step_id"], "step": s["step_name"], "severity": severity,
                                             "problem": problem, "fix": fix})

    for s in steps:
        for action, target in (("on_success_action", "on_success_step_id"), ("on_fail_action", "on_fail_step_id")):
            if s.get(action) == 4 and s.get(target) not in ids[s["job"]]:
                add(s, f"{action.split('_action')[0].replace('_', ' ')} goes to step {s[target]}, which does not exist",
                    "change the step's action in SQL Server Agent")
        if s["subsystem"] != "SSIS":
            continue
        cmd = parse_ssis_command(s["command"] or "")
        if cmd.get("source") != "catalog":
            continue
        if not same_server(cmd.get("server"), server_name):
            add(s, f"runs on another server ({cmd['server']}): not checked", "run jobs --check against that server",
                "note")
            continue
        folder, project_name, package_name = cmd["folder"], cmd["project"], cmd["package"]
        key = (folder.lower(), project_name.lower())
        if key not in projects:
            projects[key] = _Project(catalog, folder, project_name)
        project = projects[key]
        if project.found is None:
            what = f"project '{project_name}' in folder '{folder}'" if project.folder_found else f"folder '{folder}'"
            add(s, f"runs \\SSISDB\\{folder}\\{project_name}\\{package_name}, but there is no {what} in the catalog",
                "point the step at the deployed project, or deploy it")
            continue
        package = project.package(package_name)
        if package is None:
            close = _close(package_name, project.packages)
            add(s, f"runs {package_name}, which is not deployed in {folder}\\{project_name}"
                + (f" (did you mean: {', '.join(close)})" if close else ""),
                "point the step at the deployed package (renamed?), or deploy it")
            continue
        ref = cmd.get("environment_reference")
        if ref is not None and ref not in {r["reference_id"] for r in project.env_refs}:
            refs = [f"{r['reference_id']} ({r.get('environment_folder_name') or '.'}\\{r['environment_name']})"
                    for r in project.env_refs]
            add(s, f"uses environment reference {ref}, which {project_name} does not have"
                + (f"; it has {', '.join(refs)}" if refs else "; it has none"),
                "pick the environment again in the step's Configuration tab")
        for name in cmd.get("parameters", {}):
            problem = _parameter_problem(name, project, package)
            if problem:
                add(s, f"sets {name}, but {problem}", "remove or rename the override in the step's Configuration tab")
    return out
