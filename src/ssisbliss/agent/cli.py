"""ssisbliss: SQL Server Agent jobs, numbered job chains and run history, for people and agents.

Read-only. Every command prints one JSON document on stdout. A failed SSIS step names its
execution id; `ssisbliss executions --id N` shows the failing task and what it runs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..ssis.config import Environment, load_environment
from .source import SqlAgent
from .status import chains, job_views, runs

# In --help of jobs, chain, history and executions: where an agent looks just before its first query.
TROUBLESHOOT = ("Why did a job or nightly load fail? Start with `ssisbliss chain --env ENV`: it says where the chain "
                "is (completed, running: X, failed at X, stopped after X), even when nothing failed. Then "
                "`ssisbliss history --env ENV \"JOB\" --runs 1` gives the failed step and its execution id (an earlier "
                "night: --runs 5), and `ssisbliss executions --env ENV --id N` gives the failing task, its error and "
                "what it runs. --failed lists only failures, of any night: it misses a job still running or a step "
                "that never started, so an empty list does not mean the load is fine.")


def troubleshooting_help(what: str) -> str:
    return f"{what[0].upper()}{what[1:]}. {TROUBLESHOOT}"


def emit(doc: dict, code: int = 0) -> int:
    json.dump(doc, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return code


def _source(a):
    if a.env:
        env = load_environment(a.env, Path(a.config) if a.config else None)
    elif a.server:
        env = Environment(name="", server=a.server)
    else:
        raise ValueError("give --env NAME or --server")
    if env.server.startswith("fake://"):
        from .fake import FakeAgent
        from ..ssis.fake import fake_location
        return FakeAgent.load(fake_location(env.server))
    return SqlAgent(env.server, a.sql_user or env.sql_user, a.trust_server_certificate or env.trust_server_certificate)


def cmd_jobs(a) -> int:
    src = _source(a)
    views = job_views(src.jobs(), src.steps())
    if a.name:
        views = [v for v in views if a.name.lower() in v["name"].lower()]
    if a.failed:
        views = [v for v in views if (v["last_run"] or {}).get("outcome") in ("failed", "canceled")]
    if a.running:
        views = [v for v in views if v["running"]]
    if a.brief:
        for v in views:
            v.pop("steps")
    return emit({"server": src.server_name(), "jobs": views})


def cmd_chain(a) -> int:
    src = _source(a)
    found = chains(job_views(src.jobs(), src.steps()))
    if a.name:
        found = [c for c in found if any(a.name.lower() in link["job"].lower() for link in c["links"])]
    return emit({"server": src.server_name(), "chains": found})


def cmd_history(a) -> int:
    src = _source(a)
    found = runs(src.history(a.job, a.runs))
    if not found:
        names = [j["name"] for j in src.jobs()]
        if a.job not in names:
            close = [n for n in names if a.job.lower() in n.lower()]
            return emit({"error": f"no job named '{a.job}'" + (f"; did you mean: {', '.join(close)}" if close else "")}, 2)
    if a.failed:
        found = [r for r in found if r.get("outcome") != "succeeded"]
    return emit({"server": src.server_name(), "job": a.job, "runs": found})


def add_commands(sub) -> None:
    """Register jobs, chain and history on a subparsers object (the ssisbliss command uses this)."""

    def target(s):
        g = s.add_argument_group("target")
        g.add_argument("--env", help="environment name from ssisbliss.toml")
        g.add_argument("--server", help="ODBC server, e.g. host\\instance or host,port")
        g.add_argument("--config", help="path to ssisbliss.toml")
        g.add_argument("--sql-user", help="SQL login; password from SSISBLISS_SQL_PASSWORD")
        g.add_argument("--trust-server-certificate", action="store_true")

    what = "jobs with state, last and next run, what each step runs, and links"
    s = sub.add_parser("jobs", help=what, description=troubleshooting_help(what))
    target(s)
    s.add_argument("--name", help="only jobs whose name contains this")
    s.add_argument("--failed", action="store_true", help="only jobs whose last run failed or was canceled")
    s.add_argument("--running", action="store_true", help="only jobs running now")
    s.add_argument("--brief", action="store_true", help="leave out the steps")
    s.set_defaults(fn=cmd_jobs)

    what = "job chains linked by sp_start_job and where the current run is"
    s = sub.add_parser("chain", help=what, description=troubleshooting_help(what))
    target(s)
    s.add_argument("--name", help="only chains with a job whose name contains this")
    s.set_defaults(fn=cmd_chain)

    what = "recent runs of a job with step outcomes and SSIS execution ids"
    s = sub.add_parser("history", help=what, description=troubleshooting_help(what))
    target(s)
    s.add_argument("job", help="exact job name")
    s.add_argument("--runs", type=int, default=5)
    s.add_argument("--failed", action="store_true", help="only runs that did not succeed")
    s.set_defaults(fn=cmd_history)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssisbliss", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    add_commands(p.add_subparsers(dest="cmd", required=True))
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        return a.fn(a)
    except (ValueError, RuntimeError) as e:
        return emit({"error": str(e)}, 2)
    except Exception as e:  # noqa: BLE001 - always answer in JSON
        return emit({"error": f"{type(e).__name__}: {e}"}, 1)


if __name__ == "__main__":
    sys.exit(main())
