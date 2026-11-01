"""ssisbliss: safe SSIS catalog deployment and SQL Agent troubleshooting for people and agents.

Every command prints one JSON document on stdout (the contract: `ssisbliss commands`). Nothing changes on a
server without --apply and the plan_id of a reviewed plan. Agents: `ssisbliss guide` has the rules and
recipes, `ssisbliss commands` every command and option as JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .commands import common
from .commands.about import GUIDE_TOPICS, cmd_commands, cmd_guide
from .commands.common import Reply, _catalog  # noqa: F401 - _catalog: used by lab tools
from .commands.deploys import (cmd_activity, cmd_build, cmd_deploy_packages, cmd_deploy_project, cmd_pull,
                               cmd_remove_packages, cmd_rollback, cmd_status, cmd_strays)
from .commands.read import cmd_executions, cmd_health, cmd_inspect, cmd_outline, cmd_slow, cmd_versions
from .commands.skill import cmd_install_skill, skill_text  # noqa: F401 - skill_text: used by the climb
from .commands.sqlcode import _oledb_providers, cmd_format, cmd_lint, cmd_sql_diff  # noqa: F401 - lab tools
from .contract import EXIT_USAGE, Parser, UsageError, envelope, usage_doc
from .deploy import EXIT_ERROR, EXIT_REFUSED
from .executions import STATUS_IDS

RECIPES = """\
Visual Studio equivalents (T = --env ENV --folder FOLDER --project PROJECT; when the developer names no
environment, use --env dev; "live" or "production" means --env prod; a folder called STAGE is not the stage
environment; folder and project may be left out: the project is the .dtproj's name, the folder your last one
for it, else the one project on the server holding the packages (not with --apply), shown in target_from):
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
  slow, not failed: history --runs 10 marks a step `slower`; slow --env ENV --id N names the tasks

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
Read-only: inspect, versions, outline, jobs, chain, history, executions, slow, health, activity, strays.
executions --failed lists only failures, of any night: it misses a job still running or a step that
never started, so an empty list does not mean the load is fine; start from chain.
"""

def parser() -> argparse.ArgumentParser:
    p = Parser(prog="ssisbliss", description=__doc__, epilog=RECIPES,
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

    what = ("which tasks made a package run slower than usual: each task's time against its median over "
            "earlier successful runs, with row counts when the runs logged them")
    s = sub.add_parser("slow", help=what, description=what + ". Read-only. Task times need logging level Basic "
                       "or higher; row counts Performance or Verbose.")
    target(s, required=False)
    s.add_argument("--id", type=int, help="the run to explain (the 'Execution ID' in job history); default: the "
                                           "package's newest finished run")
    s.add_argument("--package", help="the package, when no --id is given")
    s.add_argument("--runs", type=int, default=20, help="runs to compare with (default 20)")
    s.set_defaults(fn=cmd_slow)

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

    s = sub.add_parser("commands", help="every command and option as JSON, and the output contract (for agents)")
    s.add_argument("name", nargs="?", help="one command: its arguments in full")
    s.set_defaults(fn=cmd_commands)

    s = sub.add_parser("guide", help="the rules and recipes an agent follows (the ssisbliss skill), as JSON")
    s.add_argument("topic", nargs="?", default="all", choices=["all", *GUIDE_TOPICS])
    s.set_defaults(fn=cmd_guide)

    s = sub.add_parser("mcp", help="serve every command as an MCP tool over stdio (for agents without a shell)")
    s.set_defaults(fn=None)
    return p



def run(argv: list[str]) -> Reply:
    """Run one command and return its answer, as printed (the CLI and the MCP server both use this)."""
    common._target_from.clear()
    common._argv[:] = argv
    p = parser()
    a = None
    try:
        a = p.parse_args(argv)
        if a.fn is None:
            raise ValueError("`ssisbliss mcp` is a server: start it from your MCP client's configuration")
        r = a.fn(a)
    except UsageError as e:
        names = list(next(x for x in p._actions if isinstance(x, argparse._SubParsersAction)).choices)
        r = Reply(usage_doc(e, argv, names), EXIT_USAGE)
    except (ValueError, RuntimeError) as e:
        r = Reply({"error": str(e)}, EXIT_REFUSED)
    except Exception as e:  # noqa: BLE001 - always answer in JSON
        r = Reply({"error": f"{type(e).__name__}: {e}"}, EXIT_ERROR)
        if type(e).__module__ == "pyodbc":
            r.doc["next"] = ["check the server name, the network and the login (ssisbliss.toml, --sql-user, "
                             "SSISBLISS_SQL_PASSWORD); `ssisbliss inspect` with the same --env tests the connection"]
    if r.doc is not None:
        r.doc = envelope(r.doc, r.code, a, common._target_from)
    return r


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["mcp"] and "-h" not in argv and "--help" not in argv:
        from .mcp import serve
        return serve()
    r = run(argv)
    if r.text is not None:
        sys.stdout.write(r.text)
    else:
        json.dump(r.doc, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    return r.code


if __name__ == "__main__":
    sys.exit(main())
