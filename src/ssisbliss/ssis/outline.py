"""What a package does, task by task, in run order: SQL statements, the processes it starts
(PowerShell, Python, ...), data flows, containers, and the databases its connections point at. Read-only,
from the .dtsx XML (through the package model)."""
from __future__ import annotations

from .model import KINDS, Package, ProjectInfo, Task, process  # noqa: F401 - KINDS: what outline calls each task

SQLTASK = "{www.microsoft.com/sqlserver/dts/tasks/sqltask}"


def outline(data: bytes | Package, project: ProjectInfo | None = None) -> dict:
    p = data if isinstance(data, Package) else Package.parse(data, project)
    conns = [{"name": c.name, "type": c.kind, "connection_string": c.connection_string or None,
              "expression": c.expressions.get("ConnectionString"), **({"database": c.database} if c.database else {}),
              **({"database_from": f"{c.assumed} in your files; the catalog may set another value"}
                 if c.assumed else {})}
             for c in p.connections if c.scope == "Package" and c.dtsid]
    return {"package": p.name, "connections": sorted(conns, key=lambda c: c["name"] or ""),
            "tasks": [_task(t) for t in p.tasks]}


def flatten(tasks: list[dict]) -> list[dict]:
    out = []
    for t in tasks:
        out.append(t)
        out.extend(flatten(t.get("tasks", [])))
    return out


def _task(t: Task) -> dict:
    out: dict = {"name": t.name, "path": t.path, "kind": t.kind, "type": t.type}
    if t.disabled:
        out["disabled"] = True
    if t.kind == "sql" and t.sql_data is not None:
        out["connection"] = t.connection.name if t.connection else t.connection_ref
        out["sql"] = t.sql
        if t.sql_source != "DirectInput":
            out["sql_source"] = t.sql_source   # FileConnection or Variable: the statement is elsewhere
    elif t.kind == "process":
        data = t.element.find("{www.microsoft.com/SqlServer/Dts}ObjectData")
        p = data.find("ExecuteProcessData") if data is not None else None
        if p is not None:
            out.update(process(p))
    elif t.kind == "dataflow" and t.element.find("{www.microsoft.com/SqlServer/Dts}ObjectData") is not None:
        out["components"] = [{"name": c.name, "class": c.cls} for c in t.components]
    if t.kind in ("sequence", "foreach", "for"):
        out["tasks"] = [_task(x) for x in t.tasks]
    if t.runs_after:
        out["runs_after"] = t.runs_after
    if t.expressions:
        out["expressions"] = t.expressions
    return out
