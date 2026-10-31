"""`slow`: which tasks made a package run slower than usual. Pure judgement of task_durations() rows.

Each task's time in the run is compared with its median over the package's earlier successful runs, so
"last night took three hours" becomes "Load Customer took 2h10m, usually 12m". SSISDB keeps task times at
logging level Basic and up (catalog.executable_statistics); rows per data flow only at Performance or
Verbose, and then the row counts are compared too: more rows is the usual reason a data flow slows down.
"""
from __future__ import annotations

from statistics import median

SUCCEEDED = 7


def _clock(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def _runs(rows: list[dict]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for r in rows:
        run = out.setdefault(r["execution_id"], {"execution_id": r["execution_id"], "status": r["status"],
                                                 "start": r["start_time"], "duration_s": r["duration_s"], "tasks": {}})
        if r.get("execution_path"):
            run["tasks"][r["execution_path"]] = {"ms": r["ms"] or 0, "times": r.get("times") or 1,
                                                 "rows": r.get("rows_sent")}
    return out


def report(rows: list[dict], execution_id: int | None = None, top: int = 5) -> dict:
    runs = _runs(rows)
    if not runs:
        return {"verdict": "no finished runs of this package in the catalog"}
    target = runs.get(execution_id) if execution_id is not None else runs[max(runs)]
    if target is None:
        return {"verdict": f"execution {execution_id} is not a finished run of this package"}
    earlier = [r for i, r in sorted(runs.items(), reverse=True) if i < target["execution_id"] and r["status"] == SUCCEEDED]
    out: dict = {"execution_id": target["execution_id"], "start": target["start"],
                 "took": _clock(target["duration_s"] or 0), "took_s": target["duration_s"],
                 "runs": [{"execution_id": r["execution_id"], "start": r["start"], "took_s": r["duration_s"],
                           "succeeded": r["status"] == SUCCEEDED} for r in sorted(runs.values(), key=lambda r: -r["execution_id"])]}
    if len(earlier) < 2:
        out["verdict"] = f"too few earlier successful runs to compare with ({len(earlier)})"
        return out
    usual = median(r["duration_s"] or 0 for r in earlier)
    out["usual"], out["usual_s"], out["compared_with"] = _clock(usual), usual, f"median of {len(earlier)} successful runs"
    if not target["tasks"]:
        out["verdict"] = ("no task times for this run: it ran at logging level None (or was cut off), so only the "
                          "package's total is known")
        return out
    paths = set(target["tasks"])
    leaves = [p for p in paths if not any(q.startswith(p + "\\") for q in paths)]   # containers add up their tasks
    extra_run = (target["duration_s"] or 0) - usual
    slower, new = [], []
    for p in leaves:
        t = target["tasks"][p]
        before = [r["tasks"][p] for r in earlier if p in r["tasks"]]
        if len(before) < 2:
            new.append(p.rsplit("\\", 1)[-1])
            continue
        usual_ms = median(b["ms"] for b in before)
        extra = (t["ms"] - usual_ms) / 1000
        if extra < max(1.0, 0.05 * extra_run) or t["ms"] < 1.25 * usual_ms:
            continue
        row: dict = {"task": p.rsplit("\\", 1)[-1], "path": p, "took": _clock(t["ms"] / 1000), "usual": _clock(usual_ms / 1000),
                     "extra_s": round(extra, 1)}
        usual_times = median(b["times"] for b in before)
        if t["times"] != usual_times:
            row["times"] = f"ran {t['times']} times, usually {usual_times:g}"   # a loop with more iterations
        rows_before = [b["rows"] for b in before if b["rows"] is not None]
        if t["rows"] is not None and rows_before:
            usual_rows = median(rows_before)
            row["rows"] = f"{t['rows']:,} rows, usually {usual_rows:,.0f}"
            if usual_rows and t["rows"] >= 1.5 * usual_rows:
                row["why"] = "more rows than usual"
        slower.append(row)
    slower.sort(key=lambda r: -r["extra_s"])
    out["slower_tasks"] = slower[:top]
    if new:
        out["new_tasks"] = sorted(new)   # not in enough earlier runs to compare: new, or renamed
    if extra_run <= max(1.0, 0.2 * usual) and not slower:
        out["verdict"] = f"as usual: {out['took']} (usually {out['usual']})"
    elif slower:
        explained = sum(r["extra_s"] for r in slower)
        first = slower[0]
        out["verdict"] = (f"{out['took']} (usually {out['usual']}): {first['task']} took {first['took']}, usually "
                          f"{first['usual']}" + (f" ({first['why']})" if first.get("why") else "")
                          + ("" if explained >= 0.5 * extra_run else
                             "; most of the extra time is not in any task (waiting to start, validation, or a "
                             "task that logs no time)"))
    else:
        out["verdict"] = (f"{out['took']} (usually {out['usual']}), but no task is slower than usual: the time went to "
                          "starting, validation or a task that logs no time")
    if target["status"] != SUCCEEDED:
        out["note"] = "this run did not succeed; compare its error with `ssisbliss executions --id " \
                      f"{target['execution_id']}`"
    return out
