"""What the commands share: the answer they return, and the environment, catalog and target they work on."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import base
from ..catalog import SqlCatalog, connection_string
from ..config import Environment, git_uncommitted, git_unpushed, load_environment
from ..deploy import EXIT_OK, EXIT_REFUSED, Plan, apply
from ..ispac import Ispac

@dataclass
class Reply:
    """A command's answer: one JSON document and the exit code, or (install-skill --print, sql-diff --markdown) text."""
    doc: dict | None
    code: int = 0
    text: str | None = None


# Where a --folder / --project that was not given came from; shown in the command's output.
_target_from: dict[str, str] = {}
# The arguments of the command being run: a plan's `next` is the same command with --apply --plan-id.
_argv: list[str] = []


def reply(doc: dict, code: int = EXIT_OK) -> Reply:
    return Reply(doc, code)


def _apply_command(plan_id: str, extra: list[str] | None = None) -> str:
    import shlex
    args, skip = [], False
    for x in _argv:
        if skip:
            skip = False
        elif x in ("--plan-id", "--confirm-env"):
            skip = True
        elif x != "--apply" and not x.startswith(("--plan-id=", "--confirm-env=")):
            args.append(x)
    return "ssisbliss " + shlex.join([*args, "--apply", "--plan-id", plan_id, *(extra or [])])


def _env(a) -> Environment:
    if a.env:
        return load_environment(a.env, Path(a.config) if a.config else None)
    if not a.server:
        raise ValueError("give --env NAME or --server")
    return Environment(name="", server=a.server, sql_user=a.sql_user,
                       trust_server_certificate=a.trust_server_certificate)


def _catalog(env: Environment, a) -> SqlCatalog:
    if env.server.startswith("fake://"):
        from ..fake import FakeCatalog, fake_location
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


def _target_from_server(a, cat, directory: Path, ask: str) -> None:
    """For a target the command could not work out: the one project on the server that holds the packages in
    `directory` fills it in, shown in target_from and named in the plan's `next`. An --apply names its target
    itself, so it is refused like any other case: `ask`, plus what to pass, from the projects on the server."""
    try:
        rows = [(r["folder"], r["project"]) for r in cat.list_projects(a.folder or None) if r.get("project")
                and (not a.project or r["project"].lower() == a.project.lower())]
        local = {f.name.lower() for f in directory.glob("*.dtsx")}
        holding = [(f, p) for f, p in rows if local & {n.lower() for n in cat.packages(cat.project(f, p).project_id)}]
    except Exception:   # noqa: BLE001 - only a hint: the error it adds to must still reach the developer
        raise ValueError(ask) from None
    if len(holding) == 1 and not getattr(a, "apply", False):
        why = f"(the only project on the server holding the packages in {directory})"
        for option, value in zip(("folder", "project"), holding[0]):
            if not getattr(a, option):
                setattr(a, option, value)
                _target_from[option] = f"{value} {why}"
                _argv.extend([f"--{option}", value])
        return
    raise ValueError(ask + _targets_on_server(directory, rows, holding))


def _targets_on_server(directory: Path, rows: list, holding: list) -> str:
    if len(holding) == 1:
        folder, project = holding[0]
        return f". The packages in {directory} are in folder {folder}, project {project} on the server: add --folder {folder} --project {project}"
    found = holding or rows
    if not found:
        return ""
    return (f". {'Projects holding the packages in ' + str(directory) if holding else 'Projects'} on the server "
            f"(folder/project): {', '.join(f'{f}/{p}' for f, p in found)}; add the right one as --folder F --project P")


def _target(a, env: Environment, cat, directory: Path, dtproj: Path | None = None) -> None:
    """Fill in --project and --folder when they were not given, the way the developer would answer Visual
    Studio's deploy wizard: the project is the .dtproj's name; the folder is where you last deployed or
    pulled that project on this server from this repository, else the environment's folder in
    ssisbliss.toml. The server is never guessed. Each choice and its source is shown in the output."""
    if not a.project:
        dtproj = dtproj or _dtproj(directory)
        if dtproj is None:
            _target_from_server(a, cat, directory, f"give --project{'' if a.folder else ' and --folder'}: no Visual "
                                                   f"Studio project (.dtproj) in {directory} or above it")
        else:
            from ..build import load_dtproj
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
            _target_from_server(a, cat, directory,
                                f"give --folder: you have not deployed or pulled {a.project} on {server} from this "
                                f"repository, and {'environment ' + env.name if env.name else '--server'} has no "
                                "folder in ssisbliss.toml")


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
            from ...agent.fake import FakeAgent
            from ..fake import fake_location
            loc = fake_location(env.server)
            return FakeAgent.load(loc) if (Path(loc) / "msdb.json").exists() else None
        from ...agent.source import SqlAgent
        return SqlAgent(env.server, a.sql_user or env.sql_user, a.trust_server_certificate or env.trust_server_certificate)
    except Exception:   # noqa: BLE001 - no msdb access: activity works without job information
        return None


def _activity(directory: Path, cat, env: Environment, a, folder: str, project: str, server: Ispac | None,
              fetch: bool = False, days: float = 14, with_agent: bool = True):
    from ..activity import Activity, local_packages
    from ..gitscan import scan
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
    from ..gitscan import _git
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


def _finish(a, env: Environment, catalog: SqlCatalog, plan: Plan, after_apply=None) -> Reply:
    doc = plan.to_dict()
    if not a.apply:
        if plan.can_apply:
            doc["next"] = [_apply_command(plan.plan_id)]
            doc["apply_when"] = ("the developer asked for this change (not only for a plan or a check); otherwise "
                                 "show them the plan")
        return reply(doc, EXIT_OK if not plan.errors else EXIT_REFUSED)
    if env.confirm and a.confirm_env != env.name:
        return reply({"status": "refused", "reason": f"applying to {env.name} needs --confirm-env {env.name}",
                      "plan": doc, **({"next": [_apply_command(plan.plan_id, ["--confirm-env", env.name])]}
                                      if plan.can_apply else {})}, EXIT_REFUSED)
    if not a.plan_id:
        return reply({"status": "refused", "reason": "--apply needs --plan-id from a reviewed plan",
                      "plan": doc, **({"next": [_apply_command(plan.plan_id)]} if plan.can_apply else {})},
                     EXIT_REFUSED)
    result, code = apply(catalog, plan, a.plan_id, Path(a.backup_dir).expanduser())
    if after_apply and result.get("new_lsn") is not None:
        after_apply(result["new_lsn"])
    target = f"--env {env.name}" if env.name else f'--server "{env.server}"'
    result["rollback"] = result.get("rollback", "").replace('--server "<server>"', target)
    return reply(result, code)
