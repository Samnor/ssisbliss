"""ssisbliss: SQL Server Agent jobs, numbered job chains and run history, for people and agents.

Read-only. Every command prints one JSON document on stdout. A failed SSIS step names its
execution id; `ssisbliss executions --id N` shows the failing task and what it runs.
"""
from __future__ import annotations

from pathlib import Path

from ..ssis.commands.common import Reply, reply
from ..ssis.config import Environment, load_environment
from .checks import step_problems
from .source import SqlAgent
from .status import chains, job_views, mark_slow, runs, skipped_starts

# In --help of jobs, chain, history and executions: where an agent looks just before its first query.
TROUBLESHOOT = ("Why did a job or nightly load fail? Start with `ssisbliss chain --env ENV`: it says where the chain "
                "is (completed, running: X, failed at X, stopped after X), even when nothing failed; a `why` there is "
                "the answer. Otherwise "
                "`ssisbliss history --env ENV \"JOB\" --runs 1` gives the failed step and its execution id (an earlier "
                "night: --runs 5), and `ssisbliss executions --env ENV --id N` gives the failing task, its error and "
                "what it runs. --failed lists only failures, of any night: it misses a job still running or a step "
                "that never started, so an empty list does not mean the load is fine. Slow rather than failed: "
                "`history --runs 10` marks a step that took longer than usual (`slower`), and `ssisbliss slow --env ENV "
                "--id N` names the tasks that took the extra time.")


def troubleshooting_help(what: str) -> str:
    return f"{what[0].upper()}{what[1:]}. {TROUBLESHOOT}"


def _environment(a) -> Environment:
    if a.env:
        return load_environment(a.env, Path(a.config) if a.config else None)
    if a.server:
        return Environment(name="", server=a.server)
    raise ValueError("give --env NAME or --server")


def _source(a):
    env = _environment(a)
    if env.server.startswith("fake://"):
        from .fake import FakeAgent
        from ..ssis.fake import fake_location
        return FakeAgent.load(fake_location(env.server))
    return SqlAgent(env.server, a.sql_user or env.sql_user, a.trust_server_certificate or env.trust_server_certificate)


def cmd_jobs(a) -> Reply:
    src = _source(a)
    views = job_views(src.jobs(), src.steps())
    if a.name:
        views = [v for v in views if a.name.lower() in v["name"].lower()]
    if a.failed:
        views = [v for v in views if (v["last_run"] or {}).get("outcome") in ("failed", "canceled")]
    if a.running:
        views = [v for v in views if v["running"]]
    doc: dict = {"server": src.server_name()}
    if a.check:
        from ..ssis.commands.common import _catalog
        found = step_problems(src.steps(), _catalog(_environment(a), a), doc["server"])
        for v in views:
            if v["name"] in found:
                v["problems"] = found[v["name"]]
        errors = sum(p["severity"] == "error" for ps in found.values() for p in ps)
        doc["checked"] = (f"{errors} step problem(s) that fail the next run" if errors else
                          "every catalog step names a deployed package, parameter and environment reference")
        if a.problems:
            views = [v for v in views if "problems" in v]
    if a.brief:
        for v in views:
            v.pop("steps")
    doc["jobs"] = views
    return reply(doc)


def cmd_chain(a) -> Reply:
    src = _source(a)
    found = chains(job_views(src.jobs(), src.steps()))
    if a.name:
        found = [c for c in found if any(a.name.lower() in link["job"].lower() for link in c["links"])]
    for c in found:
        failed = next((link["job"] for link in c["links"] if link["state"] in ("failed", "canceled")), None)
        if failed and not c["verdict"].startswith("running"):
            target = f"--env {a.env}" if a.env else f'--server "{a.server}"'
            c["next"] = f'ssisbliss history {target} "{failed}" --runs 1'
    return reply({"server": src.server_name(), "chains": found})


def cmd_history(a) -> Reply:
    src = _source(a)
    found = runs(src.history(a.job, a.runs))
    if not found:
        names = [j["name"] for j in src.jobs()]
        if a.job not in names:
            close = [n for n in names if a.job.lower() in n.lower()]
            return reply({"error": f"no job named '{a.job}'" + (f"; did you mean: {', '.join(close)}" if close else "")}, 2)
    mark_slow(found)
    if a.failed:
        found = [r for r in found if r.get("outcome") != "succeeded"]
    doc: dict = {"server": src.server_name(), "job": a.job, "runs": found}
    skipped = skipped_starts([s for s in src.steps() if s["job"] == a.job])
    if skipped:
        doc["skipped_starts"] = skipped   # runs list only the steps that ran: a skipped step would look absent
    return reply(doc)


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
    s.add_argument("--check", action="store_true",
                   help="check each SSIS step against the catalog: a package, folder, /Par parameter or environment "
                        "reference that is not deployed (renamed or removed by a deploy) fails the next run; "
                        "each job gets `problems`")
    s.add_argument("--problems", action="store_true", help="with --check: only jobs with problems")
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

