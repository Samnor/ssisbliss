"""SSIS execution history and failure reports: which task failed, with what message, and what
that task runs (its SQL statement, or the PowerShell / Python / other process it starts)."""
from __future__ import annotations

import re

from .catalog import Catalog
from .dtsx import as_editable
from .ispac import Ispac
from .known_errors import explain
from .model import Package, ProjectInfo
from .outline import flatten, outline

STATUS = {1: "created", 2: "running", 3: "canceled", 4: "failed", 5: "pending", 6: "ended_unexpectedly",
          7: "succeeded", 8: "stopping", 9: "completed"}
STATUS_IDS = {v: k for k, v in STATUS.items()}
FAILED = [4, 6]
_TASK_FIELDS = ("kind", "connection", "sql", "sql_source", "runs", "executable", "arguments", "working_directory",
                "script", "stderr_variable", "ignores_exit_code", "runs_after", "disabled")


def report(catalog: Catalog, rows: list[dict], details: bool = True, max_messages: int = 5) -> list[dict]:
    # (folder, project, package) -> {"outline", "data" (the package), "managers" (the project's .conmgr)} or None
    outlines: dict[tuple[str, str, str], dict | None] = {}
    out = []
    for r in rows:
        e = {"execution_id": r["execution_id"], "folder": r["folder_name"], "project": r["project_name"],
             "package": r["package_name"], "status": STATUS.get(r["status"], str(r["status"])),
             "start_time": r["start_time"], "end_time": r["end_time"], "duration_s": r["duration_s"],
             "caller": r["caller_name"], "environment": _env(r), "server": r.get("server_name")}
        if details and r["status"] in FAILED:
            e.update(_failure(catalog, r, outlines, max_messages))
        out.append(e)
    return out


def _env(r: dict) -> str | None:
    if not r.get("environment_name"):
        return None
    return f"{r['environment_folder_name'] or r['folder_name']}/{r['environment_name']}"


def _norm(path: str | None) -> str:
    return (path or "").lstrip("\\")


def _failure(catalog: Catalog, r: dict, outlines: dict, max_messages: int) -> dict:
    events = catalog.execution_events(r["execution_id"])
    failed = catalog.failed_executables(r["execution_id"])
    tasks: dict[tuple[str, str], dict] = {}
    package_errors: list[str] = []
    for ev in events:
        path = _norm(ev["package_path"])
        msg = (ev["message"] or "").strip()
        if not path or path == "Package":
            if msg not in package_errors:
                package_errors.append(msg)
            continue
        t = tasks.setdefault((ev["package_name"], path), {"task": ev["message_source_name"],
                                                         "package": ev["package_name"], "path": path,
                                                         "messages": []})
        if ev["subcomponent_name"] and "component" not in t:
            t["component"] = ev["subcomponent_name"]
        if msg not in t["messages"]:
            t["messages"].append(msg)
    for f in failed:   # tasks that failed without an error message at this logging level
        path = _norm(f["package_path"])
        if path and path != "Package" and (f["package_name"], path) not in tasks:
            tasks[(f["package_name"], path)] = {"task": f["executable_name"], "package": f["package_name"],
                                                "path": path, "messages": []}

    params = None
    for t in tasks.values():
        # "The task, 'X', failed." adds nothing when the real error is there too.
        real = [m for m in t["messages"] if not re.search(r"The task, '.*', failed\.$", m)]
        t["messages"] = real or t["messages"]
        every = t["messages"]
        total = len(every)
        t["messages"] = every[:max_messages]
        if total > max_messages:
            t["more_messages"] = total - max_messages
        found = _find_task(catalog, r, t["package"], t["path"], outlines)
        if found:
            t.update({k: found[k] for k in _TASK_FIELDS if k in found})
            cached = outlines[(r["folder_name"], r["project_name"], t["package"])]
            if found.get("connection") or t.get("component"):
                if params is None:
                    params = catalog.execution_parameters(r["execution_id"])
                where = _run_database(cached, params, t, found, t["package"] == r["package_name"])
                if where:
                    t.update(where)
            used = _connection_parameters(cached["outline"] if cached else None, found)
            if used:
                if params is None:
                    params = catalog.execution_parameters(r["execution_id"])
                values = {}
                for p in params:
                    if p["parameter_name"] in used:
                        v = "(sensitive)" if p["sensitive"] else p["parameter_value"]
                        values[p["parameter_name"]] = f"{v} (overridden for this run)" if p["runtime_override"] else v
                if values:
                    t["connection_parameters"] = values
                overridden = sorted(("Package" if p.get("object_type") == 30 else "Project", p["parameter_name"])
                                    for p in params if p["parameter_name"] in used and p["runtime_override"])
                if overridden:
                    t["override_note"] = _override_note(overridden, r, t["package"])
            hint = _hint(found, r)
            if hint:
                t["hint"] = hint
        explained = explain(every, r.get("executed_as_name"))
        if explained:
            t["explained"] = explained
    result: dict = {"failed_tasks": list(tasks.values())}
    if package_errors:
        result["package_errors"] = package_errors[:max_messages]
        explained = explain(package_errors, r.get("executed_as_name"))
        if explained:
            result["package_errors_explained"] = explained
    if not tasks and not package_errors:
        result["note"] = ("no error messages or failed tasks recorded; the execution may have been run with "
                          "logging level None, or ended unexpectedly (check the SQL Agent job history)")
    current = catalog.project(r["folder_name"], r["project_name"])
    if current and current.last_deployed and r["start_time"] and current.last_deployed[:19] > r["start_time"][:19]:
        result["outline_note"] = (f"the project was redeployed at {current.last_deployed} (lsn "
                                  f"{current.object_version_lsn}), after this run; task details are from the "
                                  f"current version")
    return result


def _find_task(catalog: Catalog, r: dict, package: str, path: str, outlines: dict) -> dict | None:
    key = (r["folder_name"], r["project_name"], package)
    if key not in outlines:
        try:
            ispac = Ispac(catalog.get_project(r["folder_name"], r["project_name"]))
            if package in ispac.package_names:
                data = as_editable(ispac.package(package))
                outlines[key] = {"outline": outline(data), "data": data, "managers": [
                    ispac.parts[f][1] for f in ispac.connection_manager_files if f in ispac.parts]}
            else:
                outlines[key] = None
        except Exception:   # project deleted, no permission: report without task details
            outlines[key] = None
    o = outlines[key] and outlines[key]["outline"]
    if not o:
        return None
    return next((t for t in flatten(o["tasks"]) if _norm(t["path"]) == path), None)


def _run_database(cached: dict | None, params: list[dict], t: dict, found: dict, entry_point: bool) -> dict:
    """The database the failing task (or its failing data flow component) used in this run: its connection's
    expression or connection string evaluated with the parameter values the run had (execution_parameter_values,
    overrides included). Package parameters only apply to the package the execution started."""
    if not cached:
        return {}
    def of(scope: int) -> dict[str, str | None]:
        return {p["parameter_name"].lower(): None if p["sensitive"] else p["parameter_value"]
                for p in params if p.get("object_type", 20) == scope}
    package = Package.parse(cached["data"], ProjectInfo("", of(20), cached["managers"],
                                                        package_values=of(30) if entry_point else {}))
    task = next((x for x in package.every_task if _norm(x.path) == t["path"]), None)
    if task is None:
        return {}
    conn = task.connection
    if t.get("component"):
        comp = next((c for c in task.components if c.name == t["component"]), None)
        conn = comp.connection if comp else conn
    if conn is None or not conn.database:
        return {}
    return {"database": conn.database, **({"database_connection": conn.name} if conn.name != found.get("connection") else {})}


def _connection_parameters(o: dict | None, task: dict) -> set[str]:
    """Project/package parameters in the expression of the connection the task uses."""
    if not o or not task.get("connection"):
        return set()
    conn = next((c for c in o["connections"] if c["name"] == task["connection"]), None)
    return set(re.findall(r"\$(?:Project|Package)::(\w+)", (conn or {}).get("expression") or ""))


def _override_note(names: list[tuple[str, str]], r: dict, package: str) -> str:
    """Where a run-time parameter override comes from, so the fix goes to the right place."""
    pars = ", ".join(f'/Par "${scope}::{n}"' for scope, n in names)
    return (f"{', '.join(n for _, n in names)} came from the caller ({r['caller_name']}) for this run, not from the project or "
            f"environment value. A SQL Agent job step sets it in its step command ({pars};...); "
            f"`ssisbliss jobs` shows each step's ssis.parameters (look for {package}). Fix that override in the "
            f"job step, or the database it points to; redeploying the package will not change it.")


def _hint(t: dict, r: dict) -> str | None:
    if t.get("kind") != "process":
        return None
    where = f" on {r.get('machine_name') or r.get('server_name') or 'the SSIS server'}"
    cmd = f"{t.get('executable', '')} {t.get('arguments', '')}".strip()
    wd = f" in {t['working_directory']}" if t.get("working_directory") else ""
    if t.get("stderr_variable"):
        return f"the process's error output goes to variable {t['stderr_variable']}; it is not in the catalog log"
    script = f" The script ({t['script']}) is on that server, not in this working folder." if t.get("script") else ""
    return (f"SSIS logs only the exit code of a {t.get('runs', 'process')} task, not its own error output. "
            f"To see it, run as the SSIS execution account{where}: {cmd}{wd}.{script}")
