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
    """Connections show their database: from your project's parameters and connection managers next to the
    file, or for a deployed package from the catalog's values and environments."""
    from ..model import ProjectInfo
    if a.file:
        if a.folder or a.package:
            raise ValueError("give a .dtsx file, or --folder/--project/--package for a deployed package, not both")
        return reply(outline(Path(a.file).read_bytes(), ProjectInfo.from_folder(Path(a.file).parent)))
    if not (a.folder and a.project and a.package):
        raise ValueError("give a .dtsx file, or --folder, --project and --package")
    cat = _catalog(_env(a), a)
    ispac = Ispac(cat.get_project(a.folder, a.project))
    name = a.package if a.package.lower().endswith(".dtsx") else f"{a.package}.dtsx"
    if name not in ispac.package_names:
        raise ValueError(f"{name} is not in {a.folder}/{a.project}; it has {', '.join(ispac.package_names)}")
    p = cat.project(a.folder, a.project)
    info = ProjectInfo.from_ispac(ispac, "", cat.parameter_bindings(p.project_id), cat.environment_values(p.project_id)) \
        if p is not None else None
    return reply(outline(as_editable(ispac.package(name)), info))


def _labels(places: list[tuple[str, str]]) -> list[str]:
    """Names for several projects, given (where, name): each one's own name, or where/name when two share it."""
    names = [n for _, n in places]
    return [n if names.count(n) == 1 else f"{w}/{n}" for w, n in places]


def cmd_deps(a) -> Reply:
    """How packages connect, from one: what feeds it and what it feeds, link by link, across projects."""
    from .. import deps
    given = Path(a.package)
    name = given.name if given.name.lower().endswith(".dtsx") else f"{given.name}.dtsx"
    facts: dict[str, deps.Facts] = {}
    skipped: list[str] = []
    if a.env or a.server:
        env = _env(a)
        cat = _catalog(env, a)
        _target(a, env, cat, Path((a.dir or ["."])[0]))
        wanted = [(a.folder, a.project)]
        for x in a.also:
            folder, _, project = x.partition("/")
            if not project:
                raise ValueError(f"--also takes FOLDER/PROJECT, not {x!r}")
            wanted.append((folder, project))
        if a.all_projects:
            wanted += [(r["folder"], r["project"]) for r in cat.list_projects(None) if r.get("project")]
        wanted = list(dict.fromkeys(wanted))
        labels = _labels(wanted) if len(wanted) > 1 else [""] * len(wanted)
        home = labels[0]
        for (folder, project), label in zip(wanted, labels):
            p = cat.project(folder, project)
            if p is None:
                if (folder, project) == wanted[0]:
                    return reply({"error": f"project {folder}/{project} not found"}, EXIT_REFUSED)
                skipped.append(f"{folder}/{project} (not found)")
                continue
            ispac = Ispac(cat.get_project(folder, project))
            info = deps.ProjectInfo.from_ispac(ispac, label, cat.parameter_bindings(p.project_id),
                                               cat.environment_values(p.project_id))
            for n in ispac.package_names:
                f = deps.facts(n, ispac.package(n), info)
                facts[f.key] = f
        source = f"{env.name or env.server}: " + ", ".join(f"{f}/{p}" for f, p in wanted) + " (deployed)"
    else:
        in_folder = given.suffix.lower() == ".dtsx" and (given.is_file() or given.parent != Path(".")
                                                         and given.parent.is_dir())
        folders = ([given.parent] if in_folder else []) + [Path(d) for d in (a.dir or [])]
        folders = list({f.resolve(): f for f in (folders or [Path(".")])}.values())
        places = []
        for folder in folders:
            dtproj = next(iter(sorted(folder.glob("*.dtproj"))), None)
            places.append((folder.resolve().parent.name, dtproj.stem if dtproj else folder.resolve().name))
        labels = _labels(places) if len(folders) > 1 else [""] * len(folders)
        home = labels[0] if in_folder else None
        for folder, label in zip(folders, labels):
            files = sorted(folder.glob("*.dtsx"))
            if not files:
                found = _project_folders([folder], parent=folder.resolve())
                return reply({"error": f"no .dtsx files in {folder}: give the package's path, --dir with the "
                                       f"project's packages, or --env for the deployed project",
                              **({"next": [_same(a, None, None, [x for d in found for x in ("--dir", d)])]}
                                 if found else {})}, EXIT_REFUSED)
            info = deps.ProjectInfo.from_folder(folder, label)
            for file in files:
                f = deps.facts(file.name, file.read_bytes(), info)
                facts[f.key] = f
        source = ", ".join(str(f) for f in folders) + " (your files)"
        if a.project and len(folders) > 1:
            home = a.project
    candidates = [k for k, f in facts.items() if f.package.lower() == name.lower()]
    if home is not None and len(candidates) > 1:
        candidates = [k for k in candidates if facts[k].project == home] or candidates
    if not candidates:
        import difflib
        close = difflib.get_close_matches(name.lower(), {f.package.lower(): f.package for f in facts.values()}, n=3,
                                          cutoff=0.75)
        by_lower = {f.package.lower(): f.package for f in facts.values()}
        return reply({"error": f"{name} is not in {source}" + (f"; did you mean {', '.join(by_lower[c] for c in close)}?"
                                                                  if close else "") +
                               f"; it has {', '.join(sorted(facts, key=str.lower)[:40])}",
                      "next": [_same(a, a.package, str(given.with_name(by_lower[c])) if given.parent != Path(".")
                                     else by_lower[c]) for c in close]}, EXIT_REFUSED)
    if len(candidates) > 1:
        return reply({"error": f"{name} is in several projects ({', '.join(sorted(candidates))}): give its path, "
                               f"or --project with one of them",
                      "next": [_same(a, None, None, ["--project", facts[k].project]) for k in sorted(candidates)]},
                     EXIT_REFUSED)
    g = deps.graph(candidates[0], facts, depth=a.depth, direction=a.direction)
    if a.format == "mermaid":
        return Reply(None, text=deps.mermaid(g))
    if a.format == "dot":
        return Reply(None, text=deps.dot(g))
    notes, nxt = [], []
    unknown = [n["package"] for n in g["nodes"] if n.get("unknown_sql")]
    if unknown:
        notes.append(f"{', '.join(unknown)} also run SQL that is built at run time or did not parse (unknown_sql): "
                     f"links through it are not shown")
    no_db = [n["package"] for n in g["nodes"] if n.get("unknown_database")]
    if no_db:
        notes.append(f"the database of some connections in {', '.join(no_db)} is not known (unknown_database): their "
                     f"tables are matched by schema.table only, and those links are marked unsure")
    one = len({f.project for f in facts.values()}) == 1 and not (a.also or a.all_projects)
    if one:
        notes.append("one project read: links to other projects (another layer's database) need --dir for each "
                     "project folder, or --also FOLDER/PROJECT / --all-projects with --env")
    design = [e for e in g["edges"] if e.get("unsure") and "your files" in e.get("why_unsure", "")]
    if a.env or a.server:
        if one:
            nxt.append(_same(a, None, None, ["--all-projects"]))
    else:
        if one:
            others = _project_folders(folders)
            if others:
                nxt.append(_same(a, None, None, [x for d in others for x in ("--dir", d)]))
        if design or one:
            nxt += _deployed(a, given, folders[0])
    if design:
        notes.append(f"{len(design)} link(s) go through a database that is a value in your files, which the catalog "
                     f"can replace: graph the deployed projects with --env to know which are real")
    doc = {"summary": _summary(g, one), "source": source, **g, **({"skipped": skipped} if skipped else {}),
           "mermaid": deps.mermaid(g), **({"note": "; ".join(notes)} if notes else {})}
    return reply({**doc, **({"next": nxt} if nxt else {})})


def _same(a, old: str | None, new: str | None, extra: list[str] | None = None) -> str:
    """This deps command with the package replaced and options added: ready to run."""
    import shlex
    args = [new if old is not None and x == old else x for x in a.invocation.argv]
    return "ssisbliss " + shlex.join([*args, *(extra or [])])


def _project_folders(folders: list[Path], most: int = 8, parent: Path | None = None) -> list[str]:
    """Other SSIS project folders next to the one read (siblings, or one level further down; or below `parent`),
    relative to here."""
    import os
    mine = {f.resolve() for f in folders}
    parent = parent or folders[0].resolve().parent
    found = []
    for pattern in ("*/*.dtsx", "*/*/*.dtsx"):
        for f in sorted(parent.glob(pattern)):
            d = f.parent.resolve()
            hidden = any(x.startswith(".") or x.lower() in ("bin", "obj") for x in d.relative_to(parent).parts)
            if d not in mine and d not in found and not hidden:
                found.append(d)
        if found:
            break
    return [os.path.relpath(d) for d in found[:most]]


def _deployed(a, given: Path, folder: Path) -> list[str]:
    """The same graph from the deployed projects, one command per environment in ssisbliss.toml."""
    import os
    import shlex
    import tomllib

    from ..config import find_config
    config = Path(a.config) if getattr(a, "config", None) else find_config()
    try:
        envs = list(tomllib.loads(config.read_text()).get("environments", {})) if config else []
    except (OSError, tomllib.TOMLDecodeError):
        envs = []
    stem = given.name.removesuffix(".dtsx").removesuffix(".DTSX")
    return [f"ssisbliss deps {shlex.quote(stem)} --env {shlex.quote(e)} --dir {shlex.quote(os.path.relpath(folder))} "
            f"--all-projects" for e in envs[:3]]


def _summary(g: dict, one: bool, most: int = 6) -> str:
    """The graph in one line: what feeds the package, what it feeds, and how sure the links are."""
    def listed(nodes: list[dict]) -> str:
        sure = [n["package"] for n in nodes if not n.get("unsure")]
        maybe = [n["package"] for n in nodes if n.get("unsure")]
        out = f"{len(sure)} package(s)" + (": " + ", ".join(sure[:most]) + (f" +{len(sure) - most} more" if len(sure) > most else "")
                                if sure else "")
        if maybe:
            out += (f" (and {len(maybe)} more only through unsure links, which may not be real: "
                    + ", ".join(maybe[:most]) + (f" +{len(maybe) - most} more" if len(maybe) > most else "") + ")")
        return out
    ups = [n for n in g["nodes"] if n["role"] in ("upstream", "both")]
    downs = [n for n in g["nodes"] if n["role"] in ("downstream", "both")]
    unsure = [e for e in g["edges"] if e.get("unsure")]
    text = f"{g['package']} is fed by {listed(ups)}; it feeds {listed(downs)}"
    if unsure:
        text += f"; {len(unsure)} of {len(g['edges'])} links are unsure (see why_unsure; next resolves them)"
    if one:
        text += "; only one project was read, so links to other projects are not shown (see next)"
    return text


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

