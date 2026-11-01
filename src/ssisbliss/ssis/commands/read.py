"""Read-only catalog commands: inspect, outline, deps, executions, slow, health, versions."""
from __future__ import annotations

from pathlib import Path

from ..deploy import EXIT_REFUSED
from ..dtsx import as_editable
from ..executions import FAILED, STATUS_IDS, report
from ..ispac import Ispac
from ..outline import outline
from .common import Reply, _catalog, _env, _target, reply


def cmd_inspect(a) -> Reply:
    env = _env(a)
    cat = _catalog(env, a)
    info = cat.server_info()
    doc: dict = {"server": info.server_name, "version": info.product_version, "has_ssisdb": info.has_ssisdb}
    if info.has_ssisdb:
        doc["projects"] = cat.list_projects(a.folder)
        if a.folder and a.project:
            p = cat.project(a.folder, a.project)
            if p:
                doc["packages"] = cat.packages(p.project_id)
                doc["parameter_bindings"] = cat.parameter_bindings(p.project_id)
                doc["environment_references"] = cat.environment_references(p.project_id)
    return reply(doc)


def cmd_outline(a) -> Reply:
    if a.file:
        if a.folder or a.package:
            raise ValueError("give a .dtsx file, or --folder/--project/--package for a deployed package, not both")
        return reply(outline(Path(a.file).read_bytes()))
    if not (a.folder and a.project and a.package):
        raise ValueError("give a .dtsx file, or --folder, --project and --package")
    cat = _catalog(_env(a), a)
    ispac = Ispac(cat.get_project(a.folder, a.project))
    name = a.package if a.package.lower().endswith(".dtsx") else f"{a.package}.dtsx"
    if name not in ispac.package_names:
        raise ValueError(f"{name} is not in {a.folder}/{a.project}; it has {', '.join(ispac.package_names)}")
    return reply(outline(as_editable(ispac.package(name))))


def cmd_deps(a) -> Reply:
    """How packages connect, from one: what feeds it and what it feeds, link by link."""
    from .. import deps
    name = Path(a.package).name
    name = name if name.lower().endswith(".dtsx") else f"{name}.dtsx"
    if a.env or a.server:
        env = _env(a)
        cat = _catalog(env, a)
        _target(a, env, cat, Path(a.dir))
        if cat.project(a.folder, a.project) is None:
            return reply({"error": f"project {a.folder}/{a.project} not found"}, EXIT_REFUSED)
        ispac = Ispac(cat.get_project(a.folder, a.project))
        packages = {n: ispac.package(n) for n in ispac.package_names}
        source = f"{env.name or env.server}/{a.folder}/{a.project} (deployed)"
    else:
        given = Path(a.package)
        folder = given.parent if given.suffix.lower() == ".dtsx" and given.is_file() else Path(a.dir)
        packages = {f.name: f.read_bytes() for f in sorted(folder.glob("*.dtsx"))}
        source = f"{folder} (your files)"
        if not packages:
            raise ValueError(f"no .dtsx files in {folder}: give the package's path, --dir with the project's "
                             f"packages, or --env for the deployed project")
    facts = {n: deps.facts(n, data) for n, data in packages.items()}
    if name.lower() not in {n.lower() for n in facts}:
        return reply({"error": f"{name} is not in {source}; it has {', '.join(sorted(facts, key=str.lower))}"},
                     EXIT_REFUSED)
    g = deps.graph(name, facts, depth=a.depth, direction=a.direction)
    if a.format == "mermaid":
        return Reply(None, text=deps.mermaid(g))
    if a.format == "dot":
        return Reply(None, text=deps.dot(g))
    unknown = [n["package"] for n in g["nodes"] if n.get("unknown_sql")]
    return reply({"source": source, **g, "mermaid": deps.mermaid(g),
                  **({"note": f"{', '.join(unknown)} also run SQL that is built at run time or did not parse "
                              f"(unknown_sql): links through it are not shown"} if unknown else {})})


def cmd_executions(a) -> Reply:
    cat = _catalog(_env(a), a)
    statuses = [STATUS_IDS[s] for s in a.status] + (FAILED if a.failed else [])
    package = a.package if not a.package or a.package.lower().endswith(".dtsx") else f"{a.package}.dtsx"
    rows = cat.executions(folder=a.folder, project=a.project, package=package, statuses=statuses or None,
                          since_hours=a.since_hours, limit=a.limit, execution_id=a.id)
    return reply({"executions": report(cat, rows, details=not a.no_details, max_messages=a.max_messages)})


def cmd_slow(a) -> Reply:
    from ..timing import report
    env = _env(a)
    cat = _catalog(env, a)
    if a.id is not None:
        found = cat.executions(execution_id=a.id, limit=1)
        if not found:
            return reply({"error": f"no execution {a.id} in the catalog"}, EXIT_REFUSED)
        a.folder, a.project, package = found[0]["folder_name"], found[0]["project_name"], found[0]["package_name"]
    elif not a.package:
        return reply({"error": "give --id N (an execution, e.g. from history) or --package Pkg"}, EXIT_REFUSED)
    else:
        _target(a, env, cat, Path("."))
        package = a.package if a.package.lower().endswith(".dtsx") else f"{a.package}.dtsx"
    rows = cat.task_durations(a.folder, a.project, package, a.runs, up_to=a.id)
    doc = {"package": f"{a.folder}\\{a.project}\\{package}", **report(rows, a.id)}
    earlier = [r for r in doc.get("runs", []) if r["execution_id"] < doc.get("execution_id", 0) and r["succeeded"]]
    p = cat.project(a.folder, a.project)
    if earlier and p is not None:
        # Was the package changed between the last usual run and this one? If not, the data changed.
        since, until = str(earlier[0]["start"])[:19], str(doc["start"])[:19]
        deploys = [{"lsn": v["object_version_lsn"], "by": v["created_by"], "at": str(v["created_time"])[:19]}
                   for v in cat.versions(p.project_id) if since < str(v["created_time"])[:19] <= until]
        doc["deployed_since_last_run"] = deploys or (f"no: {a.project} was not deployed between the last usual run "
                                                     f"({since}) and this one, so the packages are the same; "
                                                     "what changed is the data or the server")
    return reply(doc)


def cmd_health(a) -> Reply:
    from ...agent.cli import _source
    from ..health import CLEANUP_JOB, judge
    cat = _catalog(_env(a), a)
    facts = cat.health_facts()
    try:
        cleanup_job = next((j for j in _source(a).jobs() if j["name"] == CLEANUP_JOB), None)
    except Exception:   # noqa: BLE001 - no msdb access: the cleanup job is not judged
        cleanup_job = False
    findings = judge(facts, cleanup_job, a.stuck_hours)
    props = facts.get("properties", {})
    doc = {"server": cat.server_info().server_name,
           "verdict": f"{sum(f['severity'] == 'warning' for f in findings)} warning(s)"
                      if any(f["severity"] == "warning" for f in findings) else "healthy",
           "findings": findings,
           "catalog": {"retention_days": props.get("RETENTION_WINDOW"),
                       "operation_cleanup": props.get("OPERATION_CLEANUP_ENABLED"),
                       "logging_level": props.get("SERVER_LOGGING_LEVEL"),
                       "max_project_versions": props.get("MAX_PROJECT_VERSIONS"),
                       **{k: facts[k] for k in ("data_mb", "log_mb", "recovery_model", "log_reuse_wait", "operations",
                                                "oldest_operation_days") if k in facts},
                       "open_executions": len(facts.get("open_executions", [])),
                       "cleanup_job": "not readable" if cleanup_job is False else
                                      "missing" if cleanup_job is None else
                                      {"enabled": bool(cleanup_job.get("enabled")),
                                       "last_start": cleanup_job.get("last_start")}}}
    missing = [k for k in ("properties", "data_mb", "operations", "open_executions") if k not in facts]
    if missing:
        doc["not_checked"] = f"this login cannot read {', '.join(missing)} (needs ssis_admin or VIEW DATABASE STATE)"
    return reply(doc)



def cmd_versions(a) -> Reply:
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path.cwd())
    p = cat.project(a.folder, a.project)
    if p is None:
        return reply({"error": f"project {a.folder}/{a.project} not found"}, EXIT_REFUSED)
    return reply({"project": a.project, "current_lsn": p.object_version_lsn, "versions": cat.versions(p.project_id)})

