"""Package design rules: what makes a package slow, or work on a developer's machine and fail under SQL Agent.

Found in the package itself, without a server: data flows that go to the server once per row (OLE DB
Command, Slowly Changing Dimension, a destination without fast load, a lookup without a cache), a Sort
that holds every row in memory, Excel through Jet (32-bit only) or ACE (an extra driver on the server),
a temp table that is gone by the next task, a connection string written into the package. With
`lint --env`, `missing_providers` checks the Excel providers against the server's installed ones.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .dtsx import DTS
from .lint import RULES, Finding

_D = f"{{{DTS}}}"
_SQLTASK = "{www.microsoft.com/sqlserver/dts/tasks/sqltask}"
_TEMP = re.compile(r"\b(?:CREATE\s+TABLE|INTO)\s+(##?\w+)", re.I)
_JET = re.compile(r"Provider\s*=\s*Microsoft\.Jet\.OLEDB\.4\.0", re.I)
_ACE = re.compile(r"Provider\s*=\s*(Microsoft\.ACE\.OLEDB\.\d+\.\d+)", re.I)
_DATA_SOURCE = re.compile(r"(?:Data Source|Server)\s*=\s*([^;]+)", re.I)
_LOCAL = {".", "(local)", "localhost", "127.0.0.1"}


def _f(rule: str, file: str, task: str, path: str, message: str, hint: str) -> Finding:
    return Finding(rule, RULES[rule][1], file, task, path, 1, 1, message, hint)


def _property(component: ET.Element, name: str) -> str | None:
    p = component.find(f"properties/property[@name='{name}']")
    return p.text if p is not None else None


class _Connection:
    def __init__(self, cm: ET.Element, project: bool):
        inner = cm.find(f"{_D}ObjectData/{_D}ConnectionManager")
        self.name = cm.get(f"{_D}ObjectName") or ""
        self.kind = cm.get(f"{_D}CreationName") or ""
        self.string = (inner.get(f"{_D}ConnectionString") if inner is not None else None) or ""
        self.retain = inner is not None and (inner.get(f"{_D}Retain") or "").lower() == "true"
        self.expressions = {e.get(f"{_D}Name", "") for e in cm.findall(f"{_D}PropertyExpression")}
        self.project = project


def _connections(root: ET.Element, project_dir: Path | None) -> dict[str, _Connection]:
    """refId and DTSID of each connection manager (the package's, then the project's .conmgr files)."""
    out: dict[str, _Connection] = {}
    found = [(cm, False) for cm in root.findall(f"{_D}ConnectionManagers/{_D}ConnectionManager")]
    for f in sorted(project_dir.glob("*.conmgr")) if project_dir else []:
        try:
            found.append((ET.fromstring(f.read_bytes()), True))
        except ET.ParseError:
            continue
    for cm, project in found:
        c = _Connection(cm, project)
        for key in (cm.get(f"{_D}refId"), cm.get(f"{_D}DTSID"), f"Project.ConnectionManagers[{c.name}]" if project else None):
            if key:
                out[key] = c
    return out


def excel_providers(data: bytes) -> dict[str, str]:
    """Excel/Access OLE DB providers the package's own connection managers use -> connection name."""
    root = ET.fromstring(data)
    out = {}
    for c in _connections(root, None).values():
        for m in (_JET.search(c.string), _ACE.search(c.string)):
            if m:
                out[m.group(1) if m.re is _ACE else "Microsoft.Jet.OLEDB.4.0"] = c.name
    return out


def missing_providers(file: str, data: bytes, installed: set[str], label: str) -> list[Finding]:
    """SSB511: an Excel/Access provider the package uses that the server does not have (installed: the
    names from xp_enum_oledb_providers, which lists the 64-bit providers SQL Agent's packages get)."""
    have = {p.lower() for p in installed}
    return [_f("SSB511", file, conn, "", f"connection {conn} uses {provider}, which is not installed on {label} "
               "(64-bit): the package fails there under SQL Agent",
               "install the 64-bit Microsoft Access Database Engine on the server, or read the file another way "
               "(save it as CSV and use a Flat File source)")
            for provider, conn in sorted(excel_providers(data).items()) if provider.lower() not in have]


def design_findings(file: str, data: bytes, project_dir: Path | None = None) -> list[Finding]:
    root = ET.fromstring(data)
    conns = _connections(root, project_dir)
    out: list[Finding] = []
    for ex in root.iter(f"{_D}Executable"):
        kind = ex.get(f"{_D}ExecutableType") or ex.get(f"{_D}CreationName") or ""
        path = ex.get(f"{_D}refId") or ""
        name = ex.get(f"{_D}ObjectName") or path.rsplit("\\", 1)[-1]
        if kind == "Microsoft.Pipeline":
            out += _data_flow(file, ex, name, path)
        elif "ExecuteSQLTask" in kind or kind == "Microsoft.SqlServer.Dts.Tasks.ExecuteSQLTask.ExecuteSQLTask":
            td = ex.find(f"{_D}ObjectData/{_SQLTASK}SqlTaskData")
            if td is None:
                continue
            sql = td.get(f"{_SQLTASK}SqlStatementSource") or ""
            conn = conns.get(td.get(f"{_SQLTASK}Connection") or "")
            temp = _TEMP.search(sql)
            if temp and conn is not None and not conn.retain:
                out.append(_f("SSB520", file, name, path,
                              f"creates temp table {temp.group(1)} on connection {conn.name}, which does not keep its "
                              "session (RetainSameConnection is False): the next task gets a new session without it",
                              f"set RetainSameConnection to True on {conn.name} (and DelayValidation on the tasks "
                              "that read the temp table), or use a real staging table"))
    for c in {id(c): c for c in conns.values()}.values():
        if c.project:
            continue
        if _JET.search(c.string):
            out.append(_f("SSB510", file, c.name, "", f"connection {c.name} uses Microsoft.Jet.OLEDB.4.0, which "
                          "exists only as 32-bit: the package works in Visual Studio and fails under SQL Agent (64-bit)",
                          "use the ACE provider (Microsoft.ACE.OLEDB.16.0) with the 64-bit Access Database Engine on "
                          "the server, or run the job step in 32-bit mode"))
        elif ace := _ACE.search(c.string):
            out.append(_f("SSB510", file, c.name, "", f"connection {c.name} uses {ace.group(1)}: "
                          "the server needs the Access Database Engine in the same bitness as the run (64-bit under "
                          "SQL Agent)", "check it with `ssisbliss lint --env ENV`, which asks the server"))
        source = _DATA_SOURCE.search(c.string)
        server = source.group(1).strip() if source else ""
        if (server and server.split("\\")[0].split(",")[0].lower() not in _LOCAL and c.kind.upper() in ("OLEDB", "ADO.NET")
                and not c.expressions & {"ConnectionString", "ServerName"}):
            out.append(_f("SSB521", file, c.name, "", f"connection {c.name} has server {server} written in the package: "
                          "every environment uses it unless the catalog overrides "
                          f"CM.{c.name}.ConnectionString there",
                          "make it a project connection manager bound to an environment variable, or set its "
                          "ConnectionString from a project parameter"))
    return out


def _data_flow(file: str, task: ET.Element, name: str, path: str) -> list[Finding]:
    out = []
    for c in task.iter("component"):
        cls, comp = c.get("componentClassID", ""), c.get("name", "")
        where = f"{name}\\{comp}"
        if cls in ("Microsoft.OLEDBCommand", "Microsoft.SCD"):
            what = "OLE DB Command" if cls == "Microsoft.OLEDBCommand" else "Slowly Changing Dimension"
            out.append(_f("SSB501", file, where, path, f"{comp} ({what}) runs one statement per row: fine for "
                          "hundreds of rows, hours for millions",
                          "load the rows into a staging table with a fast-load destination, then run one "
                          "set-based UPDATE or MERGE in an Execute SQL task"))
        elif cls == "Microsoft.OLEDBDestination" and (_property(c, "AccessMode") or "0") in ("0", "1", "2"):
            out.append(_f("SSB502", file, where, path, f"{comp} inserts one row at a time (no fast load)",
                          "set the data access mode to 'Table or view - fast load'"))
        elif cls == "Microsoft.ADONETDestination" and (_property(c, "UseBulkInsertWhenPossible") or "").lower() == "false":
            out.append(_f("SSB502", file, where, path, f"{comp} inserts one row at a time (bulk insert is off)",
                          "set UseBulkInsertWhenPossible to True"))
        elif cls == "Microsoft.Lookup" and _property(c, "CacheType") == "2":
            out.append(_f("SSB503", file, where, path, f"{comp} has no cache: it queries the server once per row",
                          "use Full cache with a query that selects only the join and returned columns "
                          "(Partial cache when the reference table is too big for memory)"))
        elif cls == "Microsoft.Sort":
            out.append(_f("SSB504", file, where, path, f"{comp} holds every row in memory before passing any on",
                          "sort in the source query (ORDER BY) and mark the source output IsSorted, with SortKeyPosition "
                          "on its columns"))
    return out
