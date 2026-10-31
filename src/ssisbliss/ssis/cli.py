"""ssisbliss: safe SSIS catalog deployment and SQL Agent troubleshooting for people and agents.

Every command prints one JSON document on stdout. Nothing changes on a server
without --apply and the plan_id of a reviewed plan.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import base
from .catalog import SqlCatalog, connection_string
from .config import Environment, git_uncommitted, git_unpushed, load_environment
from .deploy import (EXIT_ERROR, EXIT_OK, EXIT_REFUSED, Plan, apply, deploys_since, plan_packages, plan_project,
                     plan_remove, plan_rollback, resolve_only)
from .build import build
from .dtsx import as_editable, package_sha256, read_package
from .executions import FAILED, STATUS_IDS, report
from .ispac import Ispac
from .outline import outline


# Where a --folder / --project that was not given came from; shown in the command's output.
_target_from: dict[str, str] = {}


def emit(doc: dict, code: int = EXIT_OK) -> int:
    if _target_from:
        first = {k: doc[k] for k in ("error", "refused_because") if k in doc}
        doc = {**first, "target_from": dict(_target_from), **doc}
    json.dump(doc, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return code


def _env(a) -> Environment:
    if a.env:
        return load_environment(a.env, Path(a.config) if a.config else None)
    if not a.server:
        raise ValueError("give --env NAME or --server")
    return Environment(name="", server=a.server, sql_user=a.sql_user,
                       trust_server_certificate=a.trust_server_certificate)


def _catalog(env: Environment, a) -> SqlCatalog:
    if env.server.startswith("fake://"):
        from .fake import FakeCatalog, fake_location
        return FakeCatalog.load(fake_location(env.server))  # type: ignore[return-value]
    return SqlCatalog(connection_string(env.server, a.sql_user or env.sql_user,
                                        a.trust_server_certificate or env.trust_server_certificate))


def _dtproj(directory: Path) -> Path | None:
    """The Visual Studio project in `directory` or the nearest folder above it, up to the repository root."""
    d = directory.resolve()
    for _ in range(4):
        found = sorted(d.glob("*.dtproj"))
        if len(found) > 1:
            raise ValueError(f"several Visual Studio projects in {d} ({', '.join(f.name for f in found)}): pass --project")
        if found or (d / ".git").exists() or d.parent == d:
            return found[0] if found else None
        d = d.parent
    return None


def _target(a, env: Environment, cat, directory: Path, dtproj: Path | None = None) -> None:
    """Fill in --project and --folder when they were not given, the way the developer would answer Visual
    Studio's deploy wizard: the project is the .dtproj's name; the folder is where you last deployed or
    pulled that project on this server from this repository, else the environment's folder in
    ssisbliss.toml. The server is never guessed. Each choice and its source is shown in the output."""
    if not a.project:
        dtproj = dtproj or _dtproj(directory)
        if dtproj is None:
            raise ValueError(f"give --project: no Visual Studio project (.dtproj) in {directory} or above it")
        from .build import load_dtproj
        a.project = load_dtproj(dtproj).name
        _target_from["project"] = f"{a.project} (from {dtproj})"
    if not a.folder:
        server = cat.server_info().server_name
        mine = base.folders(directory, server, a.project)
        if mine:
            latest = max(mine, key=lambda f: mine[f])
            if len(mine) > 1 and list(mine.values()).count(mine[latest]) > 1:
                raise ValueError(f"give --folder: you have deployed {a.project} on {server} to several folders "
                                 f"({', '.join(sorted(mine))})")
            a.folder = latest
            others = f"; also deployed to {', '.join(sorted(f for f in mine if f != latest))}" if len(mine) > 1 else ""
            _target_from["folder"] = f"{a.folder} (where you last deployed or pulled {a.project} on {server}{others})"
        elif env.folder:
            a.folder = env.folder
            _target_from["folder"] = f"{a.folder} (folder of environment {env.name} in ssisbliss.toml)"
        else:
            raise ValueError(f"give --folder: you have not deployed or pulled {a.project} on {server} from this "
                             f"repository, and {'environment ' + env.name if env.name else '--server'} has no folder "
                             f"in ssisbliss.toml")


def _server_connection(env: Environment, a):
    """A plain connection to the environment's SQL Server (lint --env reads tables and parses SQL with it)."""
    raw = getattr(_catalog(env, a), "conn", None)
    if raw is None:
        raise ValueError(f"--env {env.name or env.server}: checking against the server needs a real SQL Server")
    return raw


def _agent(env: Environment, a):
    """The environment's SQL Agent (jobs that run packages, accounts they run as), or None."""
    try:
        if env.server.startswith("fake://"):
            from ..agent.fake import FakeAgent
            from .fake import fake_location
            loc = fake_location(env.server)
            return FakeAgent.load(loc) if (Path(loc) / "msdb.json").exists() else None
        from ..agent.source import SqlAgent
        return SqlAgent(env.server, a.sql_user or env.sql_user, a.trust_server_certificate or env.trust_server_certificate)
    except Exception:   # noqa: BLE001 - no msdb access: activity works without job information
        return None


def _activity(directory: Path, cat, env: Environment, a, folder: str, project: str, server: Ispac | None,
              fetch: bool = False, days: float = 14, with_agent: bool = True):
    from .activity import Activity, local_packages
    from .gitscan import scan
    info = cat.server_info()
    key = base.target_key(info.server_name, folder, project)
    local = local_packages(directory) if directory.is_dir() else {}
    p = cat.project(folder, project)
    return Activity(scan(directory, fetch=fetch), server=server, local=local,
                    bases=base.entries([directory / n for n in (server.package_names if server else [])], key),
                    catalog=cat, folder=folder, project=project, agent=_agent(env, a) if with_agent else None,
                    days=days, versions=cat.versions(p.project_id) if p else [])


def _wrong_project_folder(act, directory: str) -> str | None:
    """Strays are judged against the project folder on the default branch: refuse a --dir whose default-branch
    folder shares no package with the deployed project (the repository root, another project), since
    then every deployed package would look like a stray."""
    scan = act.scan
    deployed = {n.lower() for n in act.server_hashes}
    if scan is None or not scan.default or set(scan.main_packages) & deployed:
        return None
    from .gitscan import _git
    paths = (_git(scan.root, "ls-tree", "-r", "--name-only", scan.default) or "").splitlines()
    folders: dict[str, int] = {}
    for path in paths:
        folder, _, name = path.rpartition("/")
        if name.lower() in deployed:
            folders[folder] = folders.get(folder, 0) + 1
    best = max(folders, key=folders.get) if folders else None
    hint = f"; the project's packages are in {best or '.'}/ on {scan.default}: pass --dir {best or '.'}" if best is not None else ""
    return (f"--dir {directory} has none of the deployed packages on {scan.default}, so every package would look like a "
            f"stray{hint}")


def _others_working_checks(plan: Plan, directory: Path, names: list[str], cat, env: Environment, a) -> None:
    """Warnings (never errors) when someone else is changing a package being deployed, or a package
    linked to it: parallel work is normal, but the developer should know before it lands on the server."""
    if plan.server_ispac is None or not names:
        return
    try:
        act = _activity(directory, cat, env, a, plan.folder, plan.project, plan.server_ispac, with_agent=False)
    except Exception:   # noqa: BLE001 - activity is advice; never let it break a deploy plan
        return
    for n in names:
        r = act.report(n)
        if not r["summary"].startswith("nobody else"):
            plan.check(f"others_working:{n}", "warn", r["summary"] + ". Not a reason to stop: the plan can be "
                       "applied. Mention it to the developer in your answer (`ssisbliss activity` has the details)")


def _env_checks(plan: Plan, env: Environment, files: list[Path]) -> None:
    if env.name:
        plan.extra["environment"] = env.name
    if env.server_name and plan.server and plan.server.server_name.lower() != env.server_name.lower():
        plan.check("environment", "error", f"--env {env.name} expects {env.server_name} but connected to "
                                           f"{plan.server.server_name}")
    if env.require_git_committed and plan.action not in ("rollback", "remove-packages") and plan.extra.get("from_ref"):
        ref = plan.extra["from_ref"]
        if env.require_git_pushed and not ref.get("pushed"):
            plan.check("git", "error", f"{env.name} only accepts pushed commits: {ref['ref']} ({ref['commit'][:10]}) is on no "
                                       "remote branch. Do not push on the developer's behalf; tell them to push it.")
        else:
            plan.check("git", "ok", f"packages from {ref['ref']} ({ref['commit'][:10]})")
    elif env.require_git_committed and plan.action not in ("rollback", "remove-packages"):
        if not files:
            plan.check("git", "error", f"{env.name} requires packages committed in git; give package files")
        bad = git_uncommitted(files)
        if bad:
            plan.check("git", "error", f"{env.name} only accepts packages committed in git: {'; '.join(bad)}. "
                                       "Stop here: committing is the developer's decision (review, message, "
                                       "branch), so do not commit on their behalf just to pass this check. "
                                       "Tell them to commit the file(s), then re-run this plan.")
        elif files and env.require_git_pushed:
            unpushed = git_unpushed(files)
            if unpushed:
                plan.check("git", "error", f"{env.name} only accepts packages whose commit is pushed to the team's "
                                           f"remote: {'; '.join(unpushed)}. Stop here: pushing publishes the "
                                           "developer's work and is their decision, so do not push (or add a "
                                           "remote) on their behalf. Tell them to push, then re-run this plan.")
            else:
                plan.check("git", "ok", "all package files committed and pushed")
        elif files:
            plan.check("git", "ok", "all package files committed")


def _finish(a, env: Environment, catalog: SqlCatalog, plan: Plan, after_apply=None) -> int:
    if not a.apply:
        return emit(plan.to_dict(), EXIT_OK if not plan.errors else EXIT_REFUSED)
    if env.confirm and a.confirm_env != env.name:
        return emit({"status": "refused", "reason": f"applying to {env.name} needs --confirm-env {env.name}",
                     "plan": plan.to_dict()}, EXIT_REFUSED)
    if not a.plan_id:
        return emit({"status": "refused", "reason": "--apply needs --plan-id from a reviewed plan",
                     "plan": plan.to_dict()}, EXIT_REFUSED)
    result, code = apply(catalog, plan, a.plan_id, Path(a.backup_dir).expanduser())
    if after_apply and result.get("new_lsn") is not None:
        after_apply(result["new_lsn"])
    target = f"--env {env.name}" if env.name else f'--server "{env.server}"'
    result["rollback"] = result.get("rollback", "").replace('--server "<server>"', target)
    return emit(result, code)


# ----------------------------------------------------------------- commands --

def cmd_inspect(a) -> int:
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
    return emit(doc)


def cmd_outline(a) -> int:
    if a.file:
        if a.folder or a.package:
            raise ValueError("give a .dtsx file, or --folder/--project/--package for a deployed package, not both")
        return emit(outline(Path(a.file).read_bytes()))
    if not (a.folder and a.project and a.package):
        raise ValueError("give a .dtsx file, or --folder, --project and --package")
    cat = _catalog(_env(a), a)
    ispac = Ispac(cat.get_project(a.folder, a.project))
    name = a.package if a.package.lower().endswith(".dtsx") else f"{a.package}.dtsx"
    if name not in ispac.package_names:
        raise ValueError(f"{name} is not in {a.folder}/{a.project}; it has {', '.join(ispac.package_names)}")
    return emit(outline(as_editable(ispac.package(name))))


def cmd_executions(a) -> int:
    cat = _catalog(_env(a), a)
    statuses = [STATUS_IDS[s] for s in a.status] + (FAILED if a.failed else [])
    package = a.package if not a.package or a.package.lower().endswith(".dtsx") else f"{a.package}.dtsx"
    rows = cat.executions(folder=a.folder, project=a.project, package=package, statuses=statuses or None,
                          since_hours=a.since_hours, limit=a.limit, execution_id=a.id)
    return emit({"executions": report(cat, rows, details=not a.no_details, max_messages=a.max_messages)})


def cmd_health(a) -> int:
    from ..agent.cli import _source
    from .health import CLEANUP_JOB, judge
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
    return emit(doc)


def cmd_status(a) -> int:
    """Each deployed package against your base record and your local file; deploys since your version."""
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path(a.dir))
    info = cat.server_info()
    p = cat.project(a.folder, a.project)
    if p is None:
        return emit({"error": f"project {a.folder}/{a.project} not found"}, EXIT_REFUSED)
    folder = Path(a.dir)
    server = Ispac(cat.get_project(a.folder, a.project)).package_hashes()
    local = {f.name: f for f in folder.glob("*.dtsx") if not f.name.endswith(".server.dtsx")}
    # Catalog package names ignore case (as deploy-packages does): stage_customer.dtsx is the deployed Stage_Customer.dtsx.
    for name in [n for n in local if n not in server]:
        same = [n for n in server if n.lower() == name.lower() and n not in local]
        if len(same) == 1:
            local[same[0]] = local.pop(name)
    key = base.target_key(info.server_name, a.folder, a.project)
    records = base.entries([folder / n for n in set(server) | set(local)], key)
    rows = []
    for name in sorted(set(server) | set(local), key=str.lower):
        row: dict = {"package": name}
        rec = records.get(name)
        if name not in server:
            row["server"] = "not deployed"
        elif rec is None:
            row["server"] = "no record of your version (pull, or deploy it once)"
        elif rec["sha256"] == server[name]:
            row["server"] = f"as you left it (your version, lsn {rec['lsn']})"
        else:
            row["server"] = f"changed since your version (lsn {rec['lsn']})"
            row["changed_by"] = deploys_since(cat.versions(p.project_id), rec["lsn"])
        if name in local and local[name].name != name:
            row["file"] = local[name].name   # deployed under the catalog's spelling of the name
        if name in local:
            row["local"] = ("same as the server" if name in server and package_sha256(local[name].read_bytes()) == server[name]
                            else "differs from the server" if name in server else "only here (new package)")
        else:
            row["local"] = f"no file in {folder}"
        rows.append(row)
    oldest = min((r["lsn"] for r in records.values() if r.get("lsn") is not None), default=None)
    rows_by = {r["package"]: r for r in rows}
    # Everything that differs counts, committed on the branch or not: git status only shows the uncommitted part.
    def edited(name: str) -> bool:   # the local file is not just the version you started from
        rec = records.get(name)
        return rec is None or package_sha256(local[name].read_bytes()) != rec["sha256"]
    differs = [r["package"] for r in rows if r["local"] in ("differs from the server", "only here (new package)")
               and edited(r["package"])]
    mine = [n for n in differs if not rows_by[n]["server"].startswith("changed since")]
    theirs = [n for n in differs if rows_by[n]["server"].startswith("changed since")]
    summary: dict = {"your_changes": mine}
    if mine:
        target = f"--env {env.name}" if env.name else f'--server "{env.server}"'
        new = " --allow-new" if any(r["server"] == "not deployed" for r in rows if r["package"] in mine) else ""
        summary["deploy_your_changes"] = (f'ssisbliss deploy-packages {target} --folder "{a.folder}" --project '
                                          f'"{a.project}"{new} ' + " ".join(str(local[n]) for n in mine))
    if theirs:
        summary["changed_by_others_too"] = theirs
    return emit({"target": key, "lsn": p.object_version_lsn, "last_deployed_by": p.deployed_by,
                 **summary, "packages": rows,
                 "deploys_since_your_oldest_version": deploys_since(cat.versions(p.project_id), oldest, limit=10)
                 if oldest is not None else None})


def skill_text() -> str:
    from importlib.resources import files
    return (files("ssisbliss") / "skills" / "ssisbliss" / "SKILL.md").read_text(encoding="utf-8")


def cmd_install_skill(a) -> int:
    """Install the ssisbliss skill at user level (nothing goes into the repository) for Claude Code, Codex and
    GitHub Copilot, which includes Copilot in VS Code (~/.copilot/skills; VS Code's local sessions also read
    ~/.claude/skills)."""
    if a.print:
        sys.stdout.write(skill_text())
        return EXIT_OK
    home = Path(a.home).expanduser() if a.home else Path.home()
    targets = {"claude": home / ".claude" / "skills", "codex": home / ".codex" / "skills",
               "copilot": home / ".copilot" / "skills"}
    chosen = [t for t in targets if getattr(a, t)] or [t for t, d in targets.items() if d.parent.exists()]
    if not chosen:
        raise ValueError("none of ~/.claude, ~/.codex or ~/.copilot exists; pass --claude, --codex or --copilot to create one")
    written = []
    for t in chosen:
        dest = targets[t] / "ssisbliss" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(skill_text(), encoding="utf-8")
        written.append(str(dest))
    return emit({"installed": written, "note": "agents pick it up in new sessions (in VS Code: a new chat)"})


def cmd_versions(a) -> int:
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path.cwd())
    p = cat.project(a.folder, a.project)
    if p is None:
        return emit({"error": f"project {a.folder}/{a.project} not found"}, EXIT_REFUSED)
    return emit({"project": a.project, "current_lsn": p.object_version_lsn, "versions": cat.versions(p.project_id)})


def cmd_pull(a) -> int:
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path(a.out))
    info = cat.server_info()
    p = cat.project(a.folder, a.project)
    if p is None:
        return emit({"error": f"project {a.folder}/{a.project} not found"}, EXIT_REFUSED)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ispac = Ispac(cat.get_project(a.folder, a.project))
    key = base.target_key(info.server_name, a.folder, a.project)
    wanted = a.packages or ispac.package_names
    unknown = [n for n in wanted if n not in ispac.package_names]
    if unknown:
        return emit({"error": f"not in the deployed project: {', '.join(unknown)}"}, EXIT_REFUSED)

    results, record = [], {}
    for name in wanted:
        server_data = as_editable(ispac.package(name))
        digest = package_sha256(server_data)
        local = out / name
        if name in a.mark_merged:
            record[name] = digest
            results.append({"package": name, "result": "marked_merged"})
        elif not local.exists():
            local.write_bytes(server_data)
            record[name] = digest
            results.append({"package": name, "result": "written"})
        elif package_sha256(local.read_bytes()) == digest:
            record[name] = digest
            results.append({"package": name, "result": "identical"})
        else:
            # Never overwrite local work: write the server copy beside it for merging.
            side = local.with_suffix(".server.dtsx")
            side.write_bytes(server_data)
            results.append({"package": name, "result": "differs", "server_copy": str(side),
                            "next": f"merge {side.name} into {name}, then: pull ... --mark-merged {name}"})
    if a.write_ispac:
        (out / f"{a.project}.deployed.ispac").write_bytes(ispac.data)
    record.update(ispac.item_hashes())   # project parameters and connection managers, for --item
    if record:
        base.record(out, key, p.object_version_lsn, record)
    return emit({"target": key, "lsn": p.object_version_lsn, "deployed_by": p.deployed_by,
                 "last_deployed": p.last_deployed, "packages": results})


def _from_ref(files: list[Path], ref: str) -> tuple[dict[str, bytes], dict]:
    """Package contents as they are in git `ref` (a branch, tag or commit), e.g. to put dev back to main."""
    import subprocess
    data, missing = {}, []
    for f in files:
        p = subprocess.run(["git", "show", f"{ref}:./{f.name}"], cwd=f.resolve().parent, capture_output=True)
        if p.returncode != 0:
            missing.append(f"{f} ({p.stderr.decode(errors='replace').strip().splitlines()[-1] if p.stderr else 'not found'})")
        else:
            data[f.name] = p.stdout
    if missing:
        raise ValueError(f"not in {ref}: {'; '.join(missing)}")
    cwd = files[0].resolve().parent
    commit = subprocess.run(["git", "rev-parse", f"{ref}^{{commit}}"], cwd=cwd, capture_output=True, text=True).stdout.strip()
    pushed = bool(subprocess.run(["git", "branch", "-r", "--contains", commit], cwd=cwd, capture_output=True,
                                 text=True).stdout.strip()) if commit else False
    return data, {"ref": ref, "commit": commit, "pushed": pushed}


def cmd_deploy_packages(a) -> int:
    env = _env(a)
    cat = _catalog(env, a)
    files = [Path(f) for f in a.files]
    _target(a, env, cat, files[0].parent)
    if a.from_ref:
        contents, ref_info = _from_ref(files, a.from_ref)
    else:
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            return emit({"error": f"files not found: {', '.join(missing)}"}, EXIT_REFUSED)
        contents, ref_info = {f.name: f.read_bytes() for f in files}, None
    info = cat.server_info()
    key = base.target_key(info.server_name, a.folder, a.project)
    bases = base.lookup(files, key)
    base_lsns = base.lsns(files, key)
    plan = plan_packages(cat, a.folder, a.project, [(f.name, contents[f.name]) for f in files],
                         allow_new=a.allow_new, allow_running=a.allow_running,
                         bases=bases, overwrite=set(a.overwrite), base_lsns=base_lsns)
    if ref_info:
        plan.extra["from_ref"] = ref_info
    elif plan.server_ispac is not None:
        _other_local_changes(plan, files, key)
    if files:
        _others_working_checks(plan, files[0].parent, [c["package"] for c in plan.to_deploy], cat, env, a)
    _env_checks(plan, env, files)

    def after(lsn: int) -> None:
        by_dir: dict[Path, dict[str, str]] = {}
        renamed = plan.extra.get("renamed", {})
        for f in files:
            name = renamed.get(f.name, f.name)
            if any(c["package"] == name for c in plan.to_deploy):
                by_dir.setdefault(f.parent, {})[name] = package_sha256(contents[f.name])
        for d, pkgs in by_dir.items():
            base.record(d, key, lsn, pkgs)

    return _finish(a, env, cat, plan, after)


def _other_local_changes(plan, files: list[Path], key: str) -> None:
    """Warn about packages next to the given ones that you changed (committed or not) but did not list."""
    given = {f.resolve() for f in files}
    server = plan.server_ispac.package_hashes()
    others = sorted({o for f in files for o in f.parent.glob("*.dtsx")
                     if o.resolve() not in given and not o.name.endswith(".server.dtsx")}, key=lambda o: o.name.lower())
    records = base.entries(others, key)
    changed = [o for o in others if o.name in records and o.name in server
               and package_sha256(o.read_bytes()) not in (records[o.name]["sha256"], server[o.name])]
    if changed:
        plan.check("other_local_changes", "warn",
                   f"also changed here since your version but not in this deploy: {', '.join(o.name for o in changed)}. "
                   f"If the developer asked to deploy all their changes, add them (ssisbliss status lists every one)")


def _ispac_base_dirs(ispac: Path) -> list[Path]:
    """Where to find base records for packages built into an .ispac, most likely first.

    SSDT builds to <project>/bin/<Configuration>/<name>.ispac, while `pull` and the .dtsx
    sources live in <project>, so look there before the directory holding the .ispac.
    """
    if ispac.parent.parent.name.lower() == "bin":
        return [ispac.parent.parent.parent, ispac.parent]
    return [ispac.parent]


def cmd_build(a) -> int:
    dtproj = Path(a.dtproj)
    data, report = build(dtproj, a.configuration)
    out = Path(a.out) if a.out else dtproj.parent / "bin" / report["configuration"] / f"{report['project']}.ispac"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    report["output"] = str(out)
    report["note"] = ("packages are included as they are, like SSISBuild.exe; Visual Studio's build would also "
                      "re-save them with its own SSIS runtime (see docs/build.md)")
    return emit(report)


def _check_build_matches_source(plan: Plan, source: Ispac, only: list[str], project_dir: Path) -> None:
    """A package taken from a built .ispac must be the project folder's .dtsx (same content and protection
    level); otherwise the build is stale or was made from files the developer does not have."""
    for name in only:
        src = project_dir / name
        if not src.is_file() or name not in source.package_names:
            continue
        built, mine = source.package(name), src.read_bytes()
        level = lambda d: read_package(name, d).protection_level  # noqa: E731
        same_level = {level(built), level(mine)} <= {0, 5} or level(built) == level(mine)
        if package_sha256(built) != package_sha256(mine) or not same_level:
            plan.check(f"source:{name}", "error",
                       f"{name} in the .ispac is not the project's {src} (different "
                       f"{'protection level' if not same_level else 'content'}): the build is stale or was made from "
                       f"other files. Rebuild from the project (or use --dtproj); do not change the project's files "
                       f"to get past this")


def _item_bases(base_dirs: list[Path], key: str, source: Ispac, items: list[str]) -> dict[str, str]:
    """Base records of project items ("param:X", "conmgr:F.conmgr"), kept like packages'."""
    ids = [source.resolve_item(n.strip()) for x in items for n in x.split(",") if n.strip()]
    out: dict[str, str] = {}
    for d in reversed(base_dirs):
        out.update(base.lookup([d / i for i in ids if i], key))
    return out


def cmd_deploy_project(a) -> int:
    env = _env(a)
    cat = _catalog(env, a)
    if bool(a.ispac) == bool(a.dtproj):
        raise ValueError("give --ispac (a built project) or --dtproj (built here, like Visual Studio would)")
    build_report = None
    if a.dtproj:
        data, build_report = build(Path(a.dtproj), a.configuration)
        source = Ispac(data)
    else:
        source = Ispac.from_file(a.ispac)
    _target(a, env, cat, Path(a.dtproj).parent if a.dtproj else _ispac_base_dirs(Path(a.ispac))[0],
            Path(a.dtproj) if a.dtproj else None)
    if a.all and a.only:
        raise ValueError("give --only PACKAGE... or --all, not both")
    if a.all:
        only = list(source.package_names)
    elif not a.only and a.item:
        only = []
    else:
        if not a.only:
            raise ValueError("name the packages to deploy with --only, or take every package in the build with --all")
        only = resolve_only(source, [n.strip() for item in a.only for n in item.split(",") if n.strip()])
    info = cat.server_info()
    key = base.target_key(info.server_name, a.folder, a.project)
    base_dirs = ([Path(a.base_dir)] if a.base_dir else
                 [Path(a.dtproj).parent] if a.dtproj else _ispac_base_dirs(Path(a.ispac)))
    bases: dict[str, str] = {}
    base_lsns: dict[str, int] = {}
    for d in reversed(base_dirs):  # earlier directories win
        bases.update(base.lookup([d / n for n in only], key))
        base_lsns.update(base.lsns([d / n for n in only], key))
    plan = plan_project(cat, a.folder, a.project, source, only, project_items=a.project_items,
                        allow_new=a.allow_new, allow_running=a.allow_running,
                        bases=bases, overwrite=set(a.overwrite), base_lsns=base_lsns,
                        items=[n.strip() for x in a.item for n in x.split(",") if n.strip()],
                        item_bases=_item_bases(base_dirs, key, source, a.item))
    plan.extra["base_dir"] = str(base_dirs[0])
    if a.ispac and not a.base_dir:
        _check_build_matches_source(plan, source, only, base_dirs[0])
    if build_report:
        plan.extra["build"] = {k: build_report[k] for k in ("dtproj", "configuration", "refreshed_metadata",
                                                            "applied_configuration", "warnings")}
    if a.all:
        plan.extra["all"] = ("every package in the build, each checked for others' changes; packages that are only "
                             "on the server are kept (Visual Studio's Deploy would remove them)")
    _others_working_checks(plan, base_dirs[0], [c["package"] for c in plan.to_deploy if c.get("package")], cat, env, a)
    # The .dtsx sources (what git tracks) live in the project folder, not beside the built .ispac.
    _env_checks(plan, env, [base_dirs[0] / n for n in only] if env.require_git_committed else [])

    def after(lsn: int) -> None:
        deployed = {c["package"]: c["sha256"] for c in plan.to_deploy}
        deployed.update(dict(plan.extra.get("items_deploy", [])))
        base.record(base_dirs[0], key, lsn, deployed)

    return _finish(a, env, cat, plan, after)


def cmd_remove_packages(a) -> int:
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path(a.dir))
    info = cat.server_info()
    key = base.target_key(info.server_name, a.folder, a.project)
    folder = Path(a.dir)
    names = [n if n.lower().endswith(".dtsx") else f"{n}.dtsx" for item in a.packages for n in item.split(",") if n]
    stray = None
    if a.strays:
        from .activity import stray_blockers, strays
        act = _activity(folder, cat, env, a, a.folder, a.project, Ispac(cat.get_project(a.folder, a.project)),
                        days=a.days)
        if not names:
            names = [r["package"] for r in strays(act, set()) if r["verdict"] == "stray"]
            if not names:
                return emit({"status": "noop", "reason": "no strays: every deployed package is in the default branch, "
                             "on a branch, called, run by a job or run lately (see `ssisbliss strays`)"})
        wrong = _wrong_project_folder(act, a.dir)
        if wrong:
            return emit({"error": wrong}, EXIT_REFUSED)
        stray = {n: stray_blockers(act, n) for n in names}
    elif not names:
        raise ValueError("name the packages to remove, or pass --strays")
    plan = plan_remove(cat, a.folder, a.project, names, bases=base.lookup([folder / n for n in names], key),
                       overwrite=set(a.overwrite), allow_running=a.allow_running,
                       base_lsns=base.lsns([folder / n for n in names], key), stray=stray)
    _env_checks(plan, env, [])

    def after(lsn: int) -> None:
        base.forget(folder, key, [c["package"] for c in plan.to_deploy])

    return _finish(a, env, cat, plan, after)


def cmd_activity(a) -> int:
    """Who else is working on these packages, or on packages linked to them."""
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path(a.dir))
    p = cat.project(a.folder, a.project)
    server = Ispac(cat.get_project(a.folder, a.project)) if p else None
    act = _activity(Path(a.dir), cat, env, a, a.folder, a.project, server, fetch=a.fetch, days=a.days)
    names = [n if n.lower().endswith(".dtsx") else f"{n}.dtsx" for item in a.packages for n in item.split(",") if n]
    doc: dict = {"target": f"{env.name or env.server}/{a.folder}/{a.project}",
                 "note": "information for the developer, not a block: when they asked for a deploy, deploy (overwriting "
                         "someone's deployed work is refused by the plan itself) and tell them who else is working where"}
    if act.scan is None:
        doc["git"] = {"note": f"{a.dir} is not in a git repository: only the server's side is shown"}
    else:
        doc["git"] = {"default_branch": act.scan.default, "branches_with_package_changes": len(act.scan.branches),
                      "fetched": act.scan.fetched, **({"note": act.scan.note} if act.scan.note else {})}
        if not a.fetch:
            doc["git"]["hint"] = "uses remote branches as of your last fetch; add --fetch to refresh them first"
    if names:
        doc["packages"] = [act.report(n) for n in names]
    else:
        everything = sorted({*act.server_hashes, *act.local}, key=str.lower)
        reports = [act.report(n) for n in everything]
        doc["packages"] = [r for r in reports if not r["summary"].startswith("nobody else")]
        doc["quiet"] = len(reports) - len(doc["packages"])
    return emit(doc)


def cmd_strays(a) -> int:
    """Deployed packages that are not in the default branch or your working copy."""
    from .activity import strays
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path(a.dir))
    if cat.project(a.folder, a.project) is None:
        return emit({"error": f"project {a.folder}/{a.project} not found"}, EXIT_REFUSED)
    server = Ispac(cat.get_project(a.folder, a.project))
    act = _activity(Path(a.dir), cat, env, a, a.folder, a.project, server, fetch=a.fetch, days=a.days)
    if act.scan is None or not act.scan.default:
        return emit({"error": f"{a.dir} is not in a git repository with a default branch; strays are packages the "
                              f"default branch does not have"}, EXIT_REFUSED)
    wrong = _wrong_project_folder(act, a.dir)
    if wrong:
        return emit({"error": wrong}, EXIT_REFUSED)
    found = strays(act, set(act.local))
    doc: dict = {"target": f"{env.name or env.server}/{a.folder}/{a.project}", "default_branch": act.scan.default,
                 "strays": found}
    removable = [r["package"] for r in found if r["verdict"] == "stray"]
    if removable:
        target = f"--env {env.name}" if env.name else f'--server "{env.server}"'
        doc["remove_command"] = (f'ssisbliss remove-packages {target} --folder "{a.folder}" --project "{a.project}" '
                                 f'--dir "{a.dir}" --strays ' + " ".join(removable))
    return emit(doc)


# ---------------------------------------------------------- lint, format, sql-diff --

def _package_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths or ["."]):
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out += [f for f in sorted(p.rglob("*.dtsx")) if not f.name.lower().endswith(".server.dtsx")
                    and not ({"bin", "obj", ".git"} & {x.lower() for x in f.relative_to(p).parts[:-1]})]
        else:
            raise ValueError(f"{p} does not exist")
    return out


def _changed_files(files: list[Path], base_ref: str | None) -> tuple[list[Path], str]:
    """The files that differ from the base (default: where this branch left the default branch), committed or not."""
    from .gitscan import _default_branch, _git
    if not files:
        return files, ""
    root_s = _git(files[0].resolve().parent, "rev-parse", "--show-toplevel")
    if not root_s:
        raise ValueError("--changed needs a git repository")
    root = Path(root_s)
    ref = base_ref or _default_branch(root)
    if not ref:
        raise ValueError("--changed: no default branch found; pass --base REF")
    base = _git(root, "merge-base", ref, "HEAD") or ref
    changed = set((_git(root, "diff", "--name-only", base) or "").splitlines())
    changed |= set((_git(root, "ls-files", "--others", "--exclude-standard") or "").splitlines())
    return [f for f in files if str(f.resolve().relative_to(root)) in changed], ref


def _baseline_path(directory: Path) -> Path:
    import hashlib
    return base.state_dir() / "lint-baseline" / (hashlib.sha256(base.repository_id(directory).encode()).hexdigest()[:20] + ".json")


def cmd_lint(a) -> int:
    from .lint import RULES, SEVERITY, lint_package, server_parse
    from .sqlsites import sites
    files = _package_files(a.paths)
    ref = ""
    if a.changed:
        files, ref = _changed_files(files, a.base)
    ignore = {r.strip().upper() for x in a.ignore for r in x.split(",") if r.strip()}
    findings = []
    for f in files:
        findings += [x for x in lint_package(str(f), f.read_bytes(), a.max_line) if x.rule not in ignore]
    if a.env:
        from .columns import ServerSchema, check
        from .lint import Finding
        env = _env(a)
        raw = _server_connection(env, a)
        label = env.name or env.server
        schema = ServerSchema(raw)
        for f in files:
            data = f.read_bytes()
            if "SSB210" not in ignore:
                for s in sites(data):
                    if s.dynamic:
                        continue
                    err = server_parse(raw, s.sql)
                    if err:
                        findings.append(Finding("SSB210", "error", str(f), s.task, s.path, 1, 1,
                                                f"{label} rejects it: {err}", "fix the SQL"))
            findings += [x for x in check(str(f), data, schema, label, f.parent) if x.rule not in ignore]
    baselined = 0
    bpath = _baseline_path(files[0].parent if files else Path("."))
    if a.update_baseline:
        bpath.parent.mkdir(parents=True, exist_ok=True)
        bpath.write_text(json.dumps(sorted({x.fingerprint() for x in findings}), indent=1))
    elif bpath.exists() and not a.no_baseline:
        known = set(json.loads(bpath.read_text()))
        baselined = sum(1 for x in findings if x.fingerprint() in known)
        findings = [x for x in findings if x.fingerprint() not in known]
    counts = {sev: sum(1 for x in findings if x.severity == sev) for sev in ("error", "warn", "info")}
    fail = SEVERITY[a.fail_on] if a.fail_on != "never" else 99
    doc: dict = {"packages": len(files), **({"compared_with": ref} if a.changed else {}), "counts": counts,
                 **({"baselined": baselined} if baselined else {}),
                 **({"baseline_written": str(bpath)} if a.update_baseline else {}),
                 "findings": [x.to_dict() for x in findings]}
    if counts["warn"] or counts["error"]:
        fixable = sorted({x.file for x in findings if x.rule == "SSB301"})
        if fixable:
            doc["format_command"] = "ssisbliss format " + " ".join(f'"{x}"' for x in fixable)
    if a.rules:
        doc = {"rules": {k: {"name": v[0], "severity": v[1], "what": v[2]} for k, v in RULES.items()}}
    return emit(doc, EXIT_REFUSED if any(SEVERITY[x.severity] >= fail for x in findings) else EXIT_OK)


def cmd_format(a) -> int:
    from .sqlformat import format_sql
    from .sqlsites import replace, sites
    files = _package_files(a.paths)
    if a.changed:
        files, _ = _changed_files(files, a.base)
    report, would_change = [], 0
    for f in files:
        data = f.read_bytes()
        found = sites(data)
        changes, skipped = {}, []
        for i, st in enumerate(found):
            if st.dynamic:
                continue
            new, why = format_sql(st.sql)
            if why:
                skipped.append({"task": st.path, "reason": why})
            elif new != st.sql:
                changes[i] = new
        entry: dict = {"file": str(f), "statements": len(found), "laid_out": len(changes)}
        if skipped:
            entry["left_as_is"] = skipped
        if changes:
            would_change += 1
            new_data = replace(data, changes, found)
            after = sites(new_data)          # the safety net: only the intended SQL changed
            ok = (len(after) == len(found) and all(after[i].sql == changes.get(i, found[i].sql) for i in range(len(found)))
                  and package_sha256(new_data) != "")
            if not ok:
                entry["error"] = "the rewritten package did not read back as intended; left unchanged"
            elif not a.check:
                f.write_bytes(new_data)
                entry["written"] = True
        report.append(entry)
    doc = {"files": len(files), "would_change" if a.check else "changed": would_change, "packages": report}
    if a.check and would_change:
        doc["fix"] = "ssisbliss format " + " ".join(f'"{e["file"]}"' for e in report if e["laid_out"])
    return emit(doc, EXIT_REFUSED if a.check and would_change else EXIT_OK)


def cmd_sql_diff(a) -> int:
    import difflib
    from .gitscan import _default_branch, _git, _git_bytes
    from .sqlformat import format_sql
    from .sqlsites import sites
    files = _package_files(a.paths)
    root_s = _git(Path(a.paths[0] if a.paths else ".").resolve() if Path(a.paths[0] if a.paths else ".").is_dir()
                  else Path(a.paths[0]).resolve().parent, "rev-parse", "--show-toplevel")
    if not root_s:
        raise ValueError("sql-diff needs a git repository")
    root = Path(root_s)
    ref = a.base or _default_branch(root)
    if not ref:
        raise ValueError("no default branch found; pass --base REF")
    merge_base = _git(root, "merge-base", ref, "HEAD") or ref
    changed = set((_git(root, "diff", "--name-only", merge_base) or "").splitlines())
    changed |= set((_git(root, "ls-files", "--others", "--exclude-standard") or "").splitlines())
    removed = [Path(root, n) for n in (_git(root, "diff", "--name-only", "--diff-filter=D", merge_base) or "").splitlines()
               if n.lower().endswith(".dtsx")]
    norm = (lambda q: format_sql(q)[0]) if a.formatted else (lambda q: q)
    packages = []
    for f in files + [r for r in removed if r not in files]:
        rel = str(f.resolve().relative_to(root)) if f.exists() else str(f.relative_to(root))
        if rel not in changed and f.exists():
            continue
        old = _git_bytes(root, "show", f"{merge_base}:{rel}")
        new = f.read_bytes() if f.exists() else None
        before = {(s.kind, s.path, s.extra.get("property", "")): s for s in (sites(old) if old else [])}
        after = {(s.kind, s.path, s.extra.get("property", "")): s for s in (sites(new) if new else [])}
        tasks = []
        for key in list(before) + [k for k in after if k not in before]:
            b, n = before.get(key), after.get(key)
            bs, ns = (norm(b.sql) if b else ""), (norm(n.sql) if n else "")
            if bs == ns:
                continue
            diff = "".join(difflib.unified_diff((bs + "\n").splitlines(True) if b else [], (ns + "\n").splitlines(True) if n else [],
                                               f"{rel} ({ref})", f"{rel}", n=3))
            tasks.append({"task": key[1], "change": "added" if not b else "removed" if not n else "changed", "diff": diff})
        if tasks or old is None or new is None:
            packages.append({"package": rel, "status": "added" if old is None else "removed" if new is None else "changed",
                             "sql_changes": tasks})
    if a.markdown:
        lines = [f"### SQL changes (compared with `{ref}`)", ""]
        if not any(p["sql_changes"] for p in packages):
            lines.append("No SQL changed.")
        for p in packages:
            for t in p["sql_changes"]:
                lines += [f"**{p['package']}** › `{t['task']}` ({t['change']})", "", "```diff", t["diff"].rstrip("\n"), "```", ""]
        sys.stdout.write("\n".join(lines).rstrip() + "\n")
        return EXIT_OK
    return emit({"compared_with": ref, "merge_base": merge_base, "formatted": bool(a.formatted), "packages": packages})


def cmd_rollback(a) -> int:
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path.cwd())
    plan = plan_rollback(cat, a.folder, a.project, a.to_lsn, allow_running=a.allow_running,
                         undo_others=a.undo_others)
    _env_checks(plan, env, [])
    return _finish(a, env, cat, plan)


# --------------------------------------------------------------------- main --

RECIPES = """\
Visual Studio equivalents (T = --env ENV --folder FOLDER --project PROJECT; when the developer names no
environment, use --env dev; a folder called STAGE is not the stage environment; folder and project may be left
out: the project is the .dtproj's name, the folder your last one for it, both shown in target_from):
  Deploy Package (Load_B)      deploy-project T --dtproj X.dtproj --only Load_B
  Rebuild + Deploy Package     the same: --dtproj builds the project first
  Deploy Package, new package  add --allow-new          (or: deploy-packages T --allow-new X/Load_C.dtsx)
  Deploy (whole project)       deploy-project T --dtproj X.dtproj --all
  one changed .dtsx            deploy-packages T X/Load_B.dtsx
  all my changes (unnamed)     status T --dir X, then its deploy_your_changes (git status misses
                               changes already committed on the branch)
  who else works on Load_B?    activity T --dir X Load_B   (others' branches, whose copy the server
                               has, runs by people, packages linked by Execute Package or tables)
  old / stray packages         strays T --dir X, then remove-packages T --dir X --strays
  check packages before a PR   lint --changed X        (lint --rules lists the rules)
  readable SQL in packages     format X/Load_B.dtsx    (only whitespace and keyword case change)
  SQL changes for a PR         sql-diff --formatted --markdown X
  (never edit SQL inside a .dtsx by hand: an attribute's line breaks must be stored as &#xA;,
   and a literal line break turns into a space when SSIS reads the file; format does it right)
  also deploy project params / connection managers   add --project-items update

Why did a job or nightly load fail? Read-only, in this order (each step's output names the next):
  1. chain --env ENV                          where the chain is, even when nothing failed: completed,
                                              running: X, failed at X, stopped after X; a `why` is the answer
  2. history --env ENV "JOB" --runs 1         the failed step and its execution id (earlier night: --runs 5)
  3. executions --env ENV --id N              the failing task, its error and what it runs

Every deploy runs twice: first without --apply (prints a plan with checks and a
plan_id; exit 0 means it can be applied), then the same command with
--apply --plan-id <plan_id> (and --confirm-env ENV where the environment asks for it).
A refused plan (exit 2) says why in its checks; a colleague's change on the server is
never overwritten: report it to the developer rather than reaching for --overwrite.
Never edit the developer's files, project settings (protection level, .dtproj) or git
history to get past a check: report the check to the developer instead.
Environments with require_git_committed / require_git_pushed (usually prod) take only what
the developer has committed / pushed themselves. Never commit, push or add a git remote on
their behalf to satisfy those settings: if a file is not committed or not pushed, stop and
tell the developer.
Someone else working on a package or a linked one (activity, or an others_working warning
in a plan) is information, not a block: deploy when the developer asked for it, and tell them who
is working where.
Read-only: inspect, versions, outline, jobs, chain, history, executions, health, activity, strays.
executions --failed lists only failures, of any night: it misses a job still running or a step that
never started, so an empty list does not mean the load is fine; start from chain.
"""

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssisbliss", description=__doc__, epilog=RECIPES,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def target(s, project=True, required=True):
        """required: the command needs a folder and project; when not given they default (see _target)."""
        g = s.add_argument_group("target")
        g.add_argument("--env", help="environment name from ssisbliss.toml")
        g.add_argument("--server", help="ODBC server, e.g. host\\instance or host,port")
        g.add_argument("--config", help="path to ssisbliss.toml")
        g.add_argument("--sql-user", help="SQL login; password from SSISBLISS_SQL_PASSWORD")
        g.add_argument("--trust-server-certificate", action="store_true")
        g.add_argument("--folder", help="SSISDB folder (default: where you last deployed or pulled this project on "
                                        "this server, else the environment's folder in ssisbliss.toml)"
                       if required else None)
        if project:
            g.add_argument("--project", help="SSISDB project (default: the name of the .dtproj here or above)"
                           if required else None)

    def applying(s):
        g = s.add_argument_group("apply")
        g.add_argument("--apply", action="store_true", help="make the change (default: plan only)")
        g.add_argument("--plan-id", help="plan_id from the reviewed plan")
        g.add_argument("--confirm-env", help="required for environments with confirm = true")
        g.add_argument("--backup-dir", default=os.environ.get("SSISBLISS_BACKUP_DIR", "~/.ssisbliss/backups"),
                       help="where pre-deploy backups go (env SSISBLISS_BACKUP_DIR)")
        g.add_argument("--allow-running", action="store_true", help="deploy while executions are running")

    s = sub.add_parser("inspect", help="list folders, projects, packages and bindings")
    target(s, required=False)
    s.set_defaults(fn=cmd_inspect)

    s = sub.add_parser("outline", help="what a package does, task by task: SQL, PowerShell, Python, data flows")
    s.add_argument("file", nargs="?", help="a .dtsx file (or use --folder/--project/--package for a deployed one)")
    target(s, required=False)
    s.add_argument("--package")
    s.set_defaults(fn=cmd_outline)

    from ..agent.cli import troubleshooting_help
    what = "recent executions; for failures: the failing task, its messages and what it runs"
    s = sub.add_parser("executions", help=what, description=troubleshooting_help(what))
    target(s, required=False)
    s.add_argument("--package")
    s.add_argument("--failed", action="store_true", help="only failed or unexpectedly ended executions")
    s.add_argument("--status", action="append", default=[], choices=sorted(STATUS_IDS), help="filter by status")
    s.add_argument("--since-hours", type=float)
    s.add_argument("--id", type=int, help="one execution (the 'Execution ID' in SQL Agent job history)")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--max-messages", type=int, default=5, help="error messages per task")
    s.add_argument("--no-details", action="store_true", help="skip messages and task details")
    s.set_defaults(fn=cmd_executions)

    what = ("catalog health: log retention and cleanup, SSISDB size and log, logging level, executions stuck "
            "as running; each warning with the T-SQL fix for the DBA")
    s = sub.add_parser("health", help=what, description=what + ". Read-only.")
    target(s, project=False, required=False)
    s.add_argument("--stuck-hours", type=float, default=24, help="an execution running this long is stuck")
    s.set_defaults(fn=cmd_health)

    s = sub.add_parser("status", help="each deployed package against your version and your local file")
    target(s)
    s.add_argument("--dir", default=".", help="folder with your .dtsx files (default: here)")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("activity", help="who else is working on packages, or on packages linked to them")
    target(s)
    s.add_argument("packages", nargs="*", help="packages to look at (default: every package someone else touches)")
    s.add_argument("--dir", default=".", help="your project folder in the git repository (default: here)")
    s.add_argument("--fetch", action="store_true", help="git fetch first, so the team's branches are current")
    s.add_argument("--days", type=float, default=14, help="how far back to look for runs by people (default 14)")
    s.set_defaults(fn=cmd_activity)

    s = sub.add_parser("strays", help="deployed packages that are not in the default branch, with the evidence")
    target(s)
    s.add_argument("--dir", default=".", help="your project folder in the git repository (default: here)")
    s.add_argument("--fetch", action="store_true", help="git fetch first, so the team's branches are current")
    s.add_argument("--days", type=float, default=30, help="a run by someone this recent means check first (default 30)")
    s.set_defaults(fn=cmd_strays)

    s = sub.add_parser("lint", help="check packages and their SQL: correctness, and SQL readable in a pull request")
    s.add_argument("paths", nargs="*", help=".dtsx files or folders (default: here, recursively; bin/obj skipped)")
    s.add_argument("--changed", action="store_true", help="only packages changed since this branch left the default branch")
    s.add_argument("--base", help="with --changed / sql-diff: compare with this ref instead of the default branch")
    s.add_argument("--ignore", action="append", default=[], metavar="RULES", help="rule ids to skip, e.g. SSB203,SSB304")
    s.add_argument("--max-line", type=int, default=120, help="longest SQL line after layout (default 120)")
    s.add_argument("--fail-on", choices=["error", "warn", "info", "never"], default="error",
                   help="exit 2 when a finding is this severe or worse (default error)")
    s.add_argument("--update-baseline", action="store_true",
                   help="accept today's findings: later runs report only new ones (kept per user, not in the repository)")
    s.add_argument("--no-baseline", action="store_true", help="report everything, including baselined findings")
    s.add_argument("--rules", action="store_true", help="list the rules")
    g = s.add_argument_group("against a server (optional)")
    g.add_argument("--env", help="also check against this environment's SQL Server: parse every statement (SET "
                                  "PARSEONLY) and compare data flow columns with its tables")
    g.add_argument("--server", help=argparse.SUPPRESS)
    g.add_argument("--config", help="path to ssisbliss.toml")
    g.add_argument("--sql-user", help="SQL login; password from SSISBLISS_SQL_PASSWORD")
    g.add_argument("--trust-server-certificate", action="store_true")
    s.set_defaults(fn=cmd_lint)

    s = sub.add_parser("format", help="lay out the SQL in packages readably (only whitespace and keyword case change)")
    s.add_argument("paths", nargs="*", help=".dtsx files or folders (default: here, recursively)")
    s.add_argument("--check", action="store_true", help="change nothing; exit 2 if any package would change")
    s.add_argument("--changed", action="store_true", help="only packages changed since this branch left the default branch")
    s.add_argument("--base", help="with --changed: compare with this ref")
    s.set_defaults(fn=cmd_format)

    s = sub.add_parser("sql-diff", help="the SQL changes in packages compared with the default branch, readable")
    s.add_argument("paths", nargs="*", help=".dtsx files or folders (default: here, recursively)")
    s.add_argument("--base", help="compare with this ref (default: where this branch left the default branch)")
    s.add_argument("--markdown", action="store_true", help="print a Markdown section for a pull request (not JSON)")
    s.add_argument("--formatted", action="store_true", help="lay out both sides first, so only real changes show")
    s.set_defaults(fn=cmd_sql_diff)

    s = sub.add_parser("install-skill", help="install the ssisbliss skill for Claude Code, Codex and GitHub Copilot "
                                             "(also in VS Code), user level")
    s.add_argument("--claude", action="store_true", help="~/.claude/skills/ssisbliss (Claude Code, VS Code local sessions)")
    s.add_argument("--codex", action="store_true", help="~/.codex/skills/ssisbliss")
    s.add_argument("--copilot", action="store_true", help="~/.copilot/skills/ssisbliss (GitHub Copilot, in VS Code and the CLI)")
    s.add_argument("--home", help=argparse.SUPPRESS)
    s.add_argument("--print", action="store_true", help="print the skill instead of installing it")
    s.set_defaults(fn=cmd_install_skill)

    s = sub.add_parser("versions", help="stored versions of a project")
    target(s)
    s.set_defaults(fn=cmd_versions)

    s = sub.add_parser("pull", help="fetch deployed packages and record them as your base")
    target(s)
    s.add_argument("--out", default=".", help="directory for the .dtsx files")
    s.add_argument("--packages", nargs="*", help="only these packages (default: all)")
    s.add_argument("--mark-merged", nargs="*", default=[], help="record the server copy as merged into yours")
    s.add_argument("--write-ispac", action="store_true", help="also save the deployed .ispac")
    s.set_defaults(fn=cmd_pull)

    s = sub.add_parser("deploy-packages", help="deploy .dtsx files into an existing project")
    target(s)
    s.add_argument("files", nargs="+")
    s.add_argument("--from-ref", metavar="REF", help="deploy the files as they are in a git branch, tag or commit "
                                                    "(e.g. main, to put dev back after your PR)")
    s.add_argument("--allow-new", action="store_true", help="allow packages not yet in the project")
    s.add_argument("--overwrite", action="append", default=[], metavar="PACKAGE",
                   help="replace this package even though its server copy changed since your base")
    applying(s)
    s.set_defaults(fn=cmd_deploy_packages)

    s = sub.add_parser("build", help="build a Visual Studio project (.dtproj) into an .ispac, without Visual Studio")
    s.add_argument("dtproj")
    s.add_argument("--configuration", "-c", help="project configuration (default: Development)")
    s.add_argument("--out", help="output .ispac (default: <project>/bin/<configuration>/<name>.ispac, like Visual Studio)")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("deploy-project", help="deploy packages from a built .ispac or a .dtproj, keeping the rest")
    target(s)
    s.add_argument("--ispac", help="a built project")
    s.add_argument("--dtproj", help="a Visual Studio project, built here first (see `build`)")
    s.add_argument("--configuration", "-c", help="with --dtproj: project configuration (default: Development)")
    s.add_argument("--only", action="append", default=[], help="package(s) to take, comma separated; the .dtsx extension is optional")
    s.add_argument("--all", action="store_true", help="take every package in the build (each still checked for "
                                                      "others' changes; server-only packages are kept)")
    s.add_argument("--item", action="append", default=[], metavar="NAME",
                   help="also take this project parameter or connection manager from the build (repeatable, comma "
                        "separated); every other project item stays as deployed")
    s.add_argument("--project-items", choices=["keep", "update"], default="keep",
                   help="project parameters / connection managers: keep deployed (default) or update")
    s.add_argument("--base-dir", help="directory holding .ssis-base.json for these packages (default: the SSDT "
                                      "project folder for <project>/bin/<Configuration>/x.ispac, then the "
                                      ".ispac's folder)")
    s.add_argument("--allow-new", action="store_true")
    s.add_argument("--overwrite", action="append", default=[], metavar="PACKAGE")
    applying(s)
    s.set_defaults(fn=cmd_deploy_project)

    s = sub.add_parser("remove-packages", help="remove packages you deployed (e.g. ones your branch added), or strays")
    target(s)
    s.add_argument("packages", nargs="*", help="package names (.dtsx optional); with --strays, default: every stray")
    s.add_argument("--dir", default=".", help="your project folder, for your base records (default: here)")
    s.add_argument("--strays", action="store_true",
                   help="remove packages that are strays (see `ssisbliss strays`), whoever deployed them")
    s.add_argument("--days", type=float, default=30, help="with --strays: a run this recent blocks removal")
    s.add_argument("--overwrite", action="append", default=[], metavar="PACKAGE",
                   help="remove even though the server copy is not your version")
    applying(s)
    s.set_defaults(fn=cmd_remove_packages)

    s = sub.add_parser("rollback", help="restore a previous project version")
    target(s)
    s.add_argument("--to-lsn", type=int, required=True)
    s.add_argument("--undo-others", action="store_true",
                   help="restore even though others deployed after that version (undoes their deploys)")
    applying(s)
    s.set_defaults(fn=cmd_rollback)

    from ..agent.cli import add_commands   # SQL Agent: jobs, chain, history
    add_commands(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    _target_from.clear()
    try:
        return a.fn(a)
    except (ValueError, RuntimeError) as e:
        return emit({"error": str(e)}, EXIT_REFUSED)
    except Exception as e:  # noqa: BLE001 - always answer in JSON
        return emit({"error": f"{type(e).__name__}: {e}"}, EXIT_ERROR)


if __name__ == "__main__":
    sys.exit(main())
