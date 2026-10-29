"""What a package does, task by task, in run order: SQL statements, the processes it starts
(PowerShell, Python, ...), data flows, containers. Read-only, from the .dtsx XML."""
from __future__ import annotations

import ntpath
import re
import shlex
import xml.etree.ElementTree as ET

from .dtsx import DTS

_D = f"{{{DTS}}}"
SQLTASK = "{www.microsoft.com/sqlserver/dts/tasks/sqltask}"

KINDS = {
    "Microsoft.ExecuteSQLTask": "sql",
    "Microsoft.ExecuteProcess": "process",
    "Microsoft.Pipeline": "dataflow",
    "Microsoft.ScriptTask": "script",
    "STOCK:SEQUENCE": "sequence",
    "STOCK:FOREACHLOOP": "foreach",
    "STOCK:FORLOOP": "for",
    "Microsoft.ExecutePackageTask": "execute_package",
    "Microsoft.FileSystemTask": "file_system",
}
# Precedence constraint DTS:Value: absent/0 = success, 1 = failure, 2 = completion.
_ON = {"0": "success", "1": "failure", "2": "completion"}


def outline(data: bytes) -> dict:
    root = ET.fromstring(data)
    conns = {}
    for cm in root.iter(f"{_D}ConnectionManager"):
        dtsid = cm.get(f"{_D}DTSID")
        if dtsid:
            inner = cm.find(f"{_D}ObjectData/{_D}ConnectionManager")
            expr = next((e.text for e in cm.findall(f"{_D}PropertyExpression")
                         if e.get(f"{_D}Name") == "ConnectionString"), None)
            conns[dtsid] = {"name": cm.get(f"{_D}ObjectName"), "type": cm.get(f"{_D}CreationName"),
                            "connection_string": inner.get(f"{_D}ConnectionString") if inner is not None else None,
                            "expression": expr}
    return {"package": root.get(f"{_D}ObjectName"),
            "connections": sorted(conns.values(), key=lambda c: c["name"] or ""),
            "tasks": _children(root, conns)}


def flatten(tasks: list[dict]) -> list[dict]:
    out = []
    for t in tasks:
        out.append(t)
        out.extend(flatten(t.get("tasks", [])))
    return out


def _children(container: ET.Element, conns: dict) -> list[dict]:
    execs = container.find(f"{_D}Executables")
    if execs is None:
        return []
    tasks = [_task(e, conns) for e in execs.findall(f"{_D}Executable")]
    after: dict[str, list[dict]] = {}
    pcs = container.find(f"{_D}PrecedenceConstraints")
    for pc in (pcs.findall(f"{_D}PrecedenceConstraint") if pcs is not None else []):
        expr = pc.get(f"{_D}Expression")
        after.setdefault(pc.get(f"{_D}To", ""), []).append(
            {"after": _name(pc.get(f"{_D}From", "")), "on": _ON.get(pc.get(f"{_D}Value", "0"), "success"),
             **({"expression": expr} if expr else {})})
    for t in tasks:
        if t["path"] in after:
            t["runs_after"] = after[t["path"]]
    return _in_run_order(tasks, after)


def _in_run_order(tasks: list[dict], after: dict[str, list[dict]]) -> list[dict]:
    """Topological order by precedence constraints; document order breaks ties."""
    by_name = {t["name"]: t for t in tasks}
    deps = {t["name"]: {a["after"] for a in after.get(t["path"], []) if a["after"] in by_name} for t in tasks}
    ordered: list[dict] = []
    done: set[str] = set()
    while len(ordered) < len(tasks):
        ready = [t for t in tasks if t["name"] not in done and deps[t["name"]] <= done]
        if not ready:   # a cycle; keep the rest in document order
            ready = [t for t in tasks if t["name"] not in done]
        ordered.append(ready[0])
        done.add(ready[0]["name"])
    return ordered


def _name(path: str) -> str:
    return path.rsplit("\\", 1)[-1]


def _task(e: ET.Element, conns: dict) -> dict:
    etype = e.get(f"{_D}ExecutableType", "")
    t: dict = {"name": e.get(f"{_D}ObjectName"), "path": e.get(f"{_D}refId"),
               "kind": KINDS.get(etype, "other"), "type": etype}
    if e.get(f"{_D}Disabled") == "True":
        t["disabled"] = True
    data = e.find(f"{_D}ObjectData")
    if t["kind"] == "sql" and data is not None:
        sql = data.find(f"{SQLTASK}SqlTaskData")
        if sql is not None:
            conn = conns.get(sql.get(f"{SQLTASK}Connection", ""), {})
            t["connection"] = conn.get("name") or sql.get(f"{SQLTASK}Connection")
            t["sql"] = sql.get(f"{SQLTASK}SqlStatementSource")
            source_type = sql.get(f"{SQLTASK}SqlStmtSourceType")
            if source_type and source_type != "DirectInput":
                t["sql_source"] = source_type   # FileConnection or Variable: the statement is elsewhere
    elif t["kind"] == "process" and data is not None:
        p = data.find("ExecuteProcessData")
        if p is not None:
            t.update(_process(p))
    elif t["kind"] == "dataflow" and data is not None:
        t["components"] = [{"name": c.get("name"), "class": c.get("componentClassID")}
                           for c in data.iter("component")]
    if t["kind"] in ("sequence", "foreach", "for"):
        t["tasks"] = _children(e, conns)
    expressions = {x.get(f"{_D}Name"): x.text for x in e.findall(f"{_D}PropertyExpression")}
    if expressions:
        t["expressions"] = expressions
    return t


def _process(p: ET.Element) -> dict:
    exe = p.get("Executable", "")
    args = p.get("Arguments", "")
    out: dict = {"executable": exe, "arguments": args, "working_directory": p.get("WorkingDirectory")}
    base = ntpath.basename(exe).lower()
    try:
        words = shlex.split(args, posix=False)
    except ValueError:
        words = args.split()
    if base in ("powershell.exe", "pwsh.exe", "powershell", "pwsh"):
        out["runs"] = "powershell"
        script = next((words[i + 1] for i, w in enumerate(words[:-1]) if w.lower() in ("-file", "-f")), None)
        if script:
            out["script"] = script.strip('"')
    elif re.match(r"(python[\d.]*|py)(\.exe)?$", base):
        out["runs"] = "python"
        script = next((w for w in words if w.strip('"').lower().endswith(".py")), None)
        if script:
            out["script"] = script.strip('"')
    elif base in ("cmd.exe", "cmd"):
        out["runs"] = "cmd"
    else:
        out["runs"] = base or "process"
    for attr, key in (("StandardErrorVariable", "stderr_variable"), ("StandardOutputVariable", "stdout_variable")):
        if p.get(attr):
            out[key] = p.get(attr)
    if p.get("FailTaskIfReturnCodeIsNotSuccessValue") == "False":
        out["ignores_exit_code"] = True
    return out
