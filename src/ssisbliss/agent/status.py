"""Job, chain and history views built from msdb rows (see source.py). Pure functions."""
from __future__ import annotations

import re

from .source import OUTCOME

_START_JOB = re.compile(r"sp_start_job\s+(?:@job_name\s*=\s*)?N?'([^']+)'", re.I)
_ISSERVER = re.compile(r'/ISSERVER\s+"?\\?"?\\SSISDB\\([^\\"]+)\\([^\\"]+)\\([^\\"]+?)\\?""?', re.I)
_SERVER = re.compile(r'/SERVER\s+(?:"\\"(.*?)\\""|"([^"]*)"|(\S+))', re.I)
_ENVREF = re.compile(r"/ENVREFERENCE\s+(\d+)", re.I)
_PAR = re.compile(r'/Par\s+"\\?"?(\$[^"\\]+?)(?:\([^)]*\))?\\?"";(?:"\\"(.*?)\\""|"([^"]*)"|(\S+))', re.I)
_FILE = re.compile(r'/(FILE|F)\s+"\\?"?([^"\\]+(?:\\[^"\\]+)*?)\\?""?', re.I)
_DTS = re.compile(r'/(DTS|SQL)\s+"\\?"?([^"]+?)\\?""?(?:\s|$)', re.I)
_EXECUTION_ID = re.compile(r"Execution ID:\s*(\d+)", re.I)
_NOT_FOUND = re.compile(r"Package path:\s*\\SSISDB\\([^\\]+)\\([^\\]+)\\([^,\s]+).*?package does not exist", re.I | re.S)


def seconds(hhmmss: int | None) -> int | None:
    if hhmmss is None:
        return None
    return hhmmss // 10000 * 3600 + hhmmss // 100 % 100 * 60 + hhmmss % 100


_BANNER = re.compile(r"Microsoft \(R\) SQL Server Execute Package Utility.*?All rights reserved\.\s*", re.S)


def clean(message: str | None, limit: int | None = None) -> str | None:
    if message is None:
        return None
    text = re.sub(r"\s+", " ", _BANNER.sub("", message)).strip()
    return text if limit is None or len(text) <= limit else text[:limit] + " ..."


def parse_ssis_command(command: str) -> dict:
    """What a SQL Agent SSIS step runs: catalog package, file, or msdb package, plus overrides."""
    out: dict = {}
    m = _ISSERVER.search(command)
    if m:
        out.update(source="catalog", folder=m.group(1), project=m.group(2), package=m.group(3))
    else:
        f = _FILE.search(command)
        d = _DTS.search(command)
        if f:
            out.update(source="file", file=f.group(2))
        elif d:
            out.update(source="msdb" if d.group(1).upper() == "SQL" else "package_store", package=d.group(2))
    s = _SERVER.search(command)
    if s:
        out["server"] = next(g for g in s.groups() if g is not None)
    e = _ENVREF.search(command)
    if e:
        out["environment_reference"] = int(e.group(1))
    params, options = {}, {}
    for p in _PAR.finditer(command):
        name, value = p.group(1), next((g for g in p.groups()[1:] if g is not None), "")
        if name.startswith("$ServerOption::"):
            options[name.removeprefix("$ServerOption::")] = value
        else:
            params[name] = value
    if params:
        out["parameters"] = params
    if options:
        out["server_options"] = options
    return out


def started_jobs(command: str | None) -> list[str]:
    return _START_JOB.findall(command or "")


def _last_run(j: dict) -> dict | None:
    if j.get("last_start") is None:
        return None
    return {"start": j["last_start"], "outcome": OUTCOME.get(j["last_status"], str(j["last_status"])),
            "duration_s": seconds(j["last_duration"]), "message": clean(j["last_message"], 300)}


def _action(code: int | None, step_id: int | None) -> str | None:
    """sysjobsteps on_success_action / on_fail_action as words."""
    return {1: "quit reporting success", 2: "quit reporting failure", 3: "go to next step",
            4: f"go to step {step_id}"}.get(code) if code is not None else None


def _step(s: dict) -> dict:
    out: dict = {"step_id": s["step_id"], "name": s["step_name"], "subsystem": s["subsystem"]}
    if s["subsystem"] == "SSIS":
        out["ssis"] = parse_ssis_command(s["command"] or "")
    else:
        out["command"] = clean(s["command"], 300)
    if s.get("database_name") and s["subsystem"] == "TSQL":
        out["database"] = s["database_name"]
    if s.get("proxy"):
        out["proxy"] = s["proxy"]
    for key, code, target in (("on_success", "on_success_action", "on_success_step_id"),
                              ("on_failure", "on_fail_action", "on_fail_step_id")):
        action = _action(s.get(code), s.get(target))
        if action:
            out[key] = action
    starts = started_jobs(s["command"])
    if starts:
        out["starts"] = starts
    return out


def _success_path(steps: list[dict]) -> list[int] | None:
    """Step ids a run passes through when every step succeeds; None if the step actions are unknown."""
    by_id = {s["step_id"]: s for s in steps}
    if not steps or any(s.get("on_success_action") is None for s in steps):
        return None
    path: list[int] = []
    current = steps[0]["step_id"]
    while current in by_id and current not in path:
        path.append(current)
        s = by_id[current]
        if s["on_success_action"] == 3:
            current = next((i for i in sorted(by_id) if i > current), None)
        elif s["on_success_action"] == 4:
            current = s["on_success_step_id"]
        else:
            break
    return path


def skipped_starts(steps: list[dict]) -> list[dict]:
    """Steps that start another job but never run when the job succeeds, with the step that ends the run."""
    path = _success_path(steps)
    if path is None:
        return []
    by_id = {s["step_id"]: s for s in steps}
    last = by_id[path[-1]]
    out = []
    for s in steps:
        starts = started_jobs(s["command"])
        if starts and s["step_id"] not in path:
            out.append({"step_id": s["step_id"], "name": s["step_name"], "would_start": starts,
                        "reason": f"step {last['step_id']} ({last['step_name']}) is set to "
                                  f"'{_action(last['on_success_action'], last['on_success_step_id'])}' on success, "
                                  f"so step {s['step_id']} ({s['step_name']}) never runs when the job succeeds"})
    return out


def job_views(jobs: list[dict], steps: list[dict]) -> list[dict]:
    by_job: dict[str, list[dict]] = {}
    for s in steps:
        by_job.setdefault(s["job"], []).append(s)
    started_by: dict[str, list[str]] = {}
    for s in steps:
        for target in started_jobs(s["command"]):
            started_by.setdefault(target, []).append(s["job"])
    out = []
    for j in jobs:
        v: dict = {"name": j["name"], "enabled": bool(j["enabled"]), "category": j["category"],
                   "running": bool(j["running"])}
        if j["running"]:
            v["running_since"] = j["running_since"]
            if j.get("last_executed_step_id"):
                v["last_completed_step"] = j["last_executed_step_id"]
        v["last_run"] = _last_run(j)
        if v["last_run"] and v["last_run"]["outcome"] in ("failed", "canceled"):
            v["next"] = f'ssisbliss history "{j["name"]}" --runs 1'
        v["schedules"] = j["enabled_schedules"]
        if j.get("next_run"):
            v["next_run"] = j["next_run"]
        v["steps"] = [_step(s) for s in by_job.get(j["name"], [])]
        starts = sorted({t for s in v["steps"] for t in s.get("starts", [])})
        if starts:
            v["starts"] = starts
        skipped = skipped_starts(by_job.get(j["name"], []))
        if skipped:
            v["skipped_starts"] = skipped   # kept in --brief: it explains a chain that stops without a failure
        if j["name"] in started_by:
            v["started_by"] = sorted(set(started_by[j["name"]]))
        out.append(v)
    return out


def chains(views: list[dict]) -> list[dict]:
    """Job chains linked by sp_start_job, each with where its current (or last) run got to."""
    by_name = {v["name"]: v for v in views}
    roots = [v for v in views if v.get("starts") and not v.get("started_by")]
    out = []
    for root in roots:
        order: list[str] = []
        stack = [root["name"]]
        while stack:
            name = stack.pop(0)
            if name in order or name not in by_name:
                continue
            order.append(name)
            stack = list(by_name[name].get("starts", [])) + stack
        out.append(_chain_status(root, [by_name[n] for n in order],
                                 [n for v in order for n in by_name[v].get("starts", []) if n not in by_name]))
    return out


def _chain_status(root: dict, links: list[dict], missing: list[str]) -> dict:
    run_start = root.get("running_since") or (root["last_run"] or {}).get("start")
    states = []
    for v in links:
        last = v["last_run"] or {}
        if v["running"]:
            state = "running"
        elif run_start and last.get("start") and last["start"] >= run_start[:19]:
            state = last["outcome"]
        else:
            state = "not_started"
        link: dict = {"job": v["name"], "state": state}
        if v["running"]:
            link["since"] = v["running_since"]
            if last.get("duration_s") is not None:
                link["last_run_took_s"] = last["duration_s"]   # how long a run usually takes
        elif last:
            link["last_start"] = last.get("start")
            link["duration_s"] = last.get("duration_s")
            if state == "failed":
                link["message"] = last.get("message")
        if not v["enabled"]:
            link["disabled"] = True
        ssis = [s["ssis"] for s in v["steps"] if s.get("ssis")]
        if ssis:
            link["runs"] = [f"{s.get('folder', '')}/{s.get('project', '')}/{s.get('package', s.get('file', ''))}"
                            for s in ssis]
        states.append(link)

    verdict: str
    running = [s for s in states if s["state"] == "running"]
    failed = [s for s in states if s["state"] in ("failed", "canceled")]
    if running:
        verdict = f"running: {', '.join(s['job'] for s in running)}"
    elif failed:
        verdict = f"{failed[0]['state']} at {failed[0]['job']}"
    elif all(s["state"] == "succeeded" for s in states):
        verdict = "completed"
    else:
        done = [s for s in states if s["state"] == "succeeded"]
        nxt = next((s for s in states if s["state"] == "not_started"), None)
        verdict = (f"stopped after {done[-1]['job']}: {nxt['job']} did not start" if done and nxt
                   else "not started")
    out: dict = {"root": root["name"], "verdict": verdict, "run_started": run_start,
                 "schedules": root["schedules"], "links": states}
    if running:
        r = running[0]
        took = f" (its last run took {r['last_run_took_s']} s)" if "last_run_took_s" in r else ""
        out["why"] = (f"{r['job']} has been running since {r['since']}{took} and has not finished; nothing in this "
                      f"chain has failed, so there is no error to find yet: history lists only finished runs, and "
                      f"the links after it wait for it. Report it as still running (stuck if it runs far longer "
                      f"than usual); stopping it is the server operator's call")
    elif verdict.startswith("stopped after"):
        by_name = {v["name"]: v for v in links}
        sk = next((sk for sk in by_name[done[-1]["job"]].get("skipped_starts", [])
                   if nxt["job"] in sk["would_start"]), None)
        if sk:
            out["why"] = (f"{done[-1]['job']}: {sk['reason']}; nothing failed. This is the whole cause, so there is no "
                          f"error, execution or deploy to look up: the fix is to change the job's on-success actions "
                          f"in SQL Server Agent so that step {sk['step_id']} ({sk['name']}) runs. history lists only "
                          f"the steps that ran")
    if root.get("next_run"):
        out["next_run"] = root["next_run"]
    if missing:
        out["missing_jobs"] = sorted(set(missing))   # started by a step but no such job exists
    return out


def _clock(s: float) -> str:
    s = int(round(s))
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}m"


def mark_slow(found: list[dict]) -> None:
    """A step that took much longer than in the other runs listed gets `slower` (and, for an SSIS step, the
    `ssisbliss slow` command that names the tasks). Needs at least two other runs of the step."""
    from statistics import median
    for i, run in enumerate(found):
        for step in run["steps"]:
            others = [s["duration_s"] for j, r in enumerate(found) if j != i for s in r["steps"]
                      if s["step_id"] == step["step_id"] and s["outcome"] == "succeeded" and s["duration_s"] is not None]
            if len(others) < 2 or step["duration_s"] is None:
                continue
            usual = median(others)
            if step["duration_s"] >= 1.5 * usual and step["duration_s"] - usual >= 60:
                step["slower"] = f"took {_clock(step['duration_s'])}, usually {_clock(usual)}"
                if "ssis_execution_id" in step and "next" not in step:
                    step["next"] = f"ssisbliss slow --id {step['ssis_execution_id']}"


def runs(rows: list[dict]) -> list[dict]:
    """Group sysjobhistory rows (newest run first) into runs with their steps."""
    out: list[dict] = []
    current: dict | None = None
    for r in rows:
        if current is None or current["run_id"] != r["run_id"]:
            current = {"run_id": r["run_id"], "start": None, "outcome": None, "duration_s": None,
                       "message": None, "steps": []}
            out.append(current)
        outcome = OUTCOME.get(r["run_status"], str(r["run_status"]))
        if r["step_id"] == 0:
            current.update(start=r["start_time"], outcome=outcome, duration_s=seconds(r["run_duration"]),
                           message=clean(r["message"], 300))
            continue
        step: dict = {"step_id": r["step_id"], "name": r["step_name"], "outcome": outcome, "start": r["start_time"],
                      "duration_s": seconds(r["run_duration"])}
        msg = r["message"] or ""
        step["message"] = clean(msg) if outcome != "succeeded" else clean(msg, 200)
        if r.get("retries_attempted"):
            step["retries"] = r["retries_attempted"]
        m = _EXECUTION_ID.search(msg)
        if m:
            step["ssis_execution_id"] = int(m.group(1))
            if outcome != "succeeded":
                step["next"] = f"ssisbliss executions --id {m.group(1)}"
        nf = _NOT_FOUND.search(msg) if outcome != "succeeded" else None
        if nf:
            folder, project, package = nf.groups()
            step["hint"] = (f"the package never started: the catalog has no {package} in {folder}/{project} (renamed "
                            f"or removed by a deploy?), or the step's login cannot see it; no SSIS execution exists. "
                            f"Compare with the deployed packages")
            step["next"] = f'ssisbliss inspect --folder "{folder}" --project "{project}"'
        current["steps"].append(step)
    for run in out:
        run.pop("run_id")
    return out
