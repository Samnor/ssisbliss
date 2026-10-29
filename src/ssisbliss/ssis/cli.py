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


def emit(doc: dict, code: int = EXIT_OK) -> int:
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


def cmd_status(a) -> int:
    """Each deployed package against your base record and your local file; deploys since your version."""
    env = _env(a)
    cat = _catalog(env, a)
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
    """Install the ssisbliss skill for Claude Code and/or Codex, user level (nothing goes into the repository)."""
    if a.print:
        sys.stdout.write(skill_text())
        return EXIT_OK
    home = Path(a.home).expanduser() if a.home else Path.home()
    targets = {"claude": home / ".claude" / "skills", "codex": home / ".codex" / "skills"}
    chosen = [t for t in ("claude", "codex") if getattr(a, t)] or [t for t, d in targets.items() if d.parent.exists()]
    if not chosen:
        raise ValueError("neither ~/.claude nor ~/.codex exists; pass --claude or --codex to create one")
    written = []
    for t in chosen:
        dest = targets[t] / "ssisbliss" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(skill_text(), encoding="utf-8")
        written.append(str(dest))
    return emit({"installed": written, "note": "agents pick it up in new sessions"})


def cmd_versions(a) -> int:
    env = _env(a)
    cat = _catalog(env, a)
    p = cat.project(a.folder, a.project)
    if p is None:
        return emit({"error": f"project {a.folder}/{a.project} not found"}, EXIT_REFUSED)
    return emit({"project": a.project, "current_lsn": p.object_version_lsn, "versions": cat.versions(p.project_id)})


def cmd_pull(a) -> int:
    env = _env(a)
    cat = _catalog(env, a)
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
    info = cat.server_info()
    key = base.target_key(info.server_name, a.folder, a.project)
    folder = Path(a.dir)
    names = [n if n.lower().endswith(".dtsx") else f"{n}.dtsx" for item in a.packages for n in item.split(",") if n]
    plan = plan_remove(cat, a.folder, a.project, names, bases=base.lookup([folder / n for n in names], key),
                       overwrite=set(a.overwrite), allow_running=a.allow_running,
                       base_lsns=base.lsns([folder / n for n in names], key))
    _env_checks(plan, env, [])

    def after(lsn: int) -> None:
        base.forget(folder, key, [c["package"] for c in plan.to_deploy])

    return _finish(a, env, cat, plan, after)


def cmd_rollback(a) -> int:
    env = _env(a)
    cat = _catalog(env, a)
    plan = plan_rollback(cat, a.folder, a.project, a.to_lsn, allow_running=a.allow_running,
                         undo_others=a.undo_others)
    _env_checks(plan, env, [])
    return _finish(a, env, cat, plan)


# --------------------------------------------------------------------- main --

RECIPES = """\
Visual Studio equivalents (T = --env ENV --folder FOLDER --project PROJECT):
  Deploy Package (Load_B)      deploy-project T --dtproj X.dtproj --only Load_B
  Rebuild + Deploy Package     the same: --dtproj builds the project first
  Deploy Package, new package  add --allow-new          (or: deploy-packages T --allow-new X/Load_C.dtsx)
  Deploy (whole project)       deploy-project T --dtproj X.dtproj --all
  one changed .dtsx            deploy-packages T X/Load_B.dtsx
  all my changes (unnamed)     status T --dir X, then its deploy_your_changes (git status misses
                               changes already committed on the branch)
  also deploy project params / connection managers   add --project-items update

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
Read-only: inspect, versions, outline, executions (failed packages: executions --failed).
"""

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssisbliss", description=__doc__, epilog=RECIPES,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def target(s, project=True, required=True):
        g = s.add_argument_group("target")
        g.add_argument("--env", help="environment name from ssisbliss.toml")
        g.add_argument("--server", help="ODBC server, e.g. host\\instance or host,port")
        g.add_argument("--config", help="path to ssisbliss.toml")
        g.add_argument("--sql-user", help="SQL login; password from SSISBLISS_SQL_PASSWORD")
        g.add_argument("--trust-server-certificate", action="store_true")
        g.add_argument("--folder", required=required)
        if project:
            g.add_argument("--project", required=required)

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

    s = sub.add_parser("executions", help="recent executions; for failures: the failing task, its messages and "
                                          "what it runs")
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

    s = sub.add_parser("status", help="each deployed package against your version and your local file")
    target(s)
    s.add_argument("--dir", default=".", help="folder with your .dtsx files (default: here)")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("install-skill", help="install the ssisbliss skill for Claude Code and/or Codex (user level)")
    s.add_argument("--claude", action="store_true", help="~/.claude/skills/ssisbliss")
    s.add_argument("--codex", action="store_true", help="~/.codex/skills/ssisbliss")
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

    s = sub.add_parser("remove-packages", help="remove packages you deployed (e.g. ones your branch added)")
    target(s)
    s.add_argument("packages", nargs="+", help="package names (.dtsx optional)")
    s.add_argument("--dir", default=".", help="your project folder, for your base records (default: here)")
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
    try:
        return a.fn(a)
    except (ValueError, RuntimeError) as e:
        return emit({"error": str(e)}, EXIT_REFUSED)
    except Exception as e:  # noqa: BLE001 - always answer in JSON
        return emit({"error": f"{type(e).__name__}: {e}"}, EXIT_ERROR)


if __name__ == "__main__":
    sys.exit(main())
