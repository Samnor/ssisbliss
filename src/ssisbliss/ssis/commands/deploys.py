"""Commands that compare, deploy, remove or roll back packages, and those that say who else works on them."""
from __future__ import annotations

import shlex
from pathlib import Path

from .. import base
from ..build import ProtectionRefused, build
from ..deploy import (EXIT_REFUSED, Plan, deploys_since, plan_packages, plan_project,
                      plan_remove, plan_rollback, resolve_only)
from ..dtsx import as_editable, package_sha256, read_package
from ..ispac import Ispac
from .common import (Reply, _activity, _catalog, _env, _env_checks, _finish,
                     _others_working_checks, _target, _wrong_project_folder, reply)


def cmd_status(a) -> Reply:
    """Each deployed package against your base record and your local file; deploys since your version."""
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path(a.dir))
    info = cat.server_info()
    p = cat.project(a.folder, a.project)
    if p is None:
        return reply({"error": f"project {a.folder}/{a.project} not found"}, EXIT_REFUSED)
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
    return reply({"target": key, "lsn": p.object_version_lsn, "last_deployed_by": p.deployed_by,
                 **summary, "packages": rows,
                 "deploys_since_your_oldest_version": deploys_since(cat.versions(p.project_id), oldest, limit=10)
                 if oldest is not None else None})


def cmd_pull(a) -> Reply:
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path(a.out))
    info = cat.server_info()
    p = cat.project(a.folder, a.project)
    if p is None:
        return reply({"error": f"project {a.folder}/{a.project} not found"}, EXIT_REFUSED)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ispac = Ispac(cat.get_project(a.folder, a.project))
    key = base.target_key(info.server_name, a.folder, a.project)
    wanted = a.packages or ispac.package_names
    unknown = [n for n in wanted if n not in ispac.package_names]
    if unknown:
        return reply({"error": f"not in the deployed project: {', '.join(unknown)}"}, EXIT_REFUSED)

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
    return reply({"target": key, "lsn": p.object_version_lsn, "deployed_by": p.deployed_by,
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


def cmd_deploy_packages(a) -> Reply:
    env = _env(a)
    cat = _catalog(env, a)
    files = [Path(f) for f in a.files]
    _target(a, env, cat, files[0].parent)
    if a.from_ref:
        contents, ref_info = _from_ref(files, a.from_ref)
    else:
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            return reply({"error": f"files not found: {', '.join(missing)}"}, EXIT_REFUSED)
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


def cmd_build(a) -> Reply:
    dtproj = Path(a.dtproj)
    try:
        data, report = build(dtproj, a.configuration)
    except ProtectionRefused as e:
        # The developer fixes this, so they should hear in one answer whatever else waits for this deploy.
        return reply({"error": str(e), "next": [f"ssisbliss status --env ENV --dir {shlex.quote(str(dtproj.parent))}"],
                      "note": "Nothing was built, and the protection level is the developer's to change, not yours. "
                              "A deploy with --dtproj is refused the same way. Before you report this, run `next` "
                              "(read-only) with the environment the developer named, and tell them what the server "
                              "holds: whether a colleague changed these packages too."}, EXIT_REFUSED)
    out = Path(a.out) if a.out else dtproj.parent / "bin" / report["configuration"] / f"{report['project']}.ispac"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    report["output"] = str(out)
    report["note"] = ("packages are included as they are, like SSISBuild.exe; Visual Studio's build would also "
                      "re-save them with its own SSIS runtime (see docs/build.md)")
    return reply(report)


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


def cmd_deploy_project(a) -> Reply:
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


def cmd_remove_packages(a) -> Reply:
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path(a.dir))
    info = cat.server_info()
    key = base.target_key(info.server_name, a.folder, a.project)
    folder = Path(a.dir)
    names = [n if n.lower().endswith(".dtsx") else f"{n}.dtsx" for item in a.packages for n in item.split(",") if n]
    stray = None
    if a.strays:
        from ..activity import stray_blockers, strays
        act = _activity(folder, cat, env, a, a.folder, a.project, Ispac(cat.get_project(a.folder, a.project)),
                        days=a.days)
        if not names:
            names = [r["package"] for r in strays(act, set()) if r["verdict"] == "stray"]
            if not names:
                return reply({"status": "noop", "reason": "no strays: every deployed package is in the default branch, "
                             "on a branch, called, run by a job or run lately (see `ssisbliss strays`)"})
        wrong = _wrong_project_folder(act, a.dir)
        if wrong:
            return reply({"error": wrong}, EXIT_REFUSED)
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


def cmd_activity(a) -> Reply:
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
    return reply(doc)


def cmd_strays(a) -> Reply:
    """Deployed packages that are not in the default branch or your working copy."""
    from ..activity import strays
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path(a.dir))
    if cat.project(a.folder, a.project) is None:
        return reply({"error": f"project {a.folder}/{a.project} not found"}, EXIT_REFUSED)
    server = Ispac(cat.get_project(a.folder, a.project))
    act = _activity(Path(a.dir), cat, env, a, a.folder, a.project, server, fetch=a.fetch, days=a.days)
    if act.scan is None or not act.scan.default:
        return reply({"error": f"{a.dir} is not in a git repository with a default branch; strays are packages the "
                              f"default branch does not have"}, EXIT_REFUSED)
    wrong = _wrong_project_folder(act, a.dir)
    if wrong:
        return reply({"error": wrong}, EXIT_REFUSED)
    found = strays(act, set(act.local))
    doc: dict = {"target": f"{env.name or env.server}/{a.folder}/{a.project}", "default_branch": act.scan.default,
                 "strays": found}
    removable = [r["package"] for r in found if r["verdict"] == "stray"]
    if removable:
        target = f"--env {env.name}" if env.name else f'--server "{env.server}"'
        doc["remove_command"] = (f'ssisbliss remove-packages {target} --folder "{a.folder}" --project "{a.project}" '
                                 f'--dir "{a.dir}" --strays ' + " ".join(removable))
    return reply(doc)


def cmd_rollback(a) -> Reply:
    env = _env(a)
    cat = _catalog(env, a)
    _target(a, env, cat, Path.cwd())
    plan = plan_rollback(cat, a.folder, a.project, a.to_lsn, allow_running=a.allow_running,
                         undo_others=a.undo_others)
    _env_checks(plan, env, [])
    return _finish(a, env, cat, plan)

