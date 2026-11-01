#!/usr/bin/env python3
"""Speed and scale benchmarks for ssisbliss: deterministic, no agents.

Builds (once per run) worlds at the sizes real estates reach and times the commands people and agents
run there, as the median of several runs: startup, lint and format on a 1.2 MB package with 2000 SQL
statements, status, a deploy plan and deps on a project with 200 packages, and activity and strays in a
repository with 40 colleague branches. For the git-heavy commands it also counts the git processes
started, which (unlike time) does not move with the machine's load.

    uv run python climb/perf.py [--out DIR] [--repeat 5]

Writes perf.json and perf.md to DIR. climb.py runs it after each proposal (see compare()).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CLIMB = Path(__file__).resolve().parent
REPO = CLIMB.parent
sys.path.insert(0, str(CLIMB))

from team import sql_world  # noqa: E402
from world import ME, SERVERS, git, task_package  # noqa: E402

BIN = REPO / ".venv" / "bin"
SQL = ("insert into stage.Customer (Id, Name, Email, Region) select c.Id, c.Name, c.Email, r.Code from bronze.Customer c "
       "join bronze.Region r on r.Id = c.RegionId where c.Active = 1 and r.Code <> 'XX' and c.Created > dateadd(day, -7, getdate())")
BRANCHES, COMMITS, BIG_PROJECT = 40, 3, 200
# A change may make a benchmark at most this much slower (and by at least SLACK_S, so tiny timings are not
# judged on noise); git process counts must not rise by more than GIT_SLACK.
SLOWER, SLACK_S, GIT_SLACK = 1.25, 0.15, 0.10
FASTER = 0.80   # a benchmark at most this fraction of the best counts as a real speed-up


def build(root: Path) -> dict[str, Path]:
    """The benchmark worlds under root: busy (activity, strays), big (status, deploy plan), and a big package."""
    from ssisbliss.ssis import base
    from ssisbliss.ssis.fake import ispac
    root.mkdir(parents=True, exist_ok=True)
    big_pkg = root / "Big.dtsx"
    big_pkg.write_bytes(task_package("Big.dtsx", sql=[SQL] * 2000))

    busy = sql_world(root / "busy")
    clone = busy.root / "clone-team"
    git(busy.root, "clone", "-q", str(busy.root / "origin.git"), str(clone))
    names = ["Stage_Customer.dtsx", "Stage_Product.dtsx", "Stage_SalesOrder.dtsx", "Main.dtsx"]
    for b in range(BRANCHES):
        who = f"Dev{b % 8}"
        env = {"GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": f"{who.lower()}@example.com",
               "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": f"{who.lower()}@example.com"}
        git(clone, "checkout", "-q", "-B", f"pr/{b}", "origin/main")
        for c in range(COMMITS):
            for name in (names[b % 4], names[(b + 1) % 4]):
                (clone / "STAGE" / name).write_bytes(task_package(name, revision=100 * b + c, sql=[SQL, f"SELECT {c}"]))
            git(clone, "commit", "-q", "-am", f"pr {b} step {c}", env=env)
        git(clone, "push", "-q", "origin", f"pr/{b}")
    git(busy.work, "fetch", "-q", "origin")
    busy.deploy_as("dev", "LAB\\alice", "Stage_Old.dtsx", task_package("Stage_Old.dtsx", sql=[SQL]))   # one stray

    big = sql_world(root / "big")
    pkgs = {f"Load_{i:03d}.dtsx": task_package(f"Load_{i:03d}.dtsx", sql=[SQL, f"DELETE FROM stage.T{i} WHERE x = ?"])
            for i in range(BIG_PROJECT)}
    folder = big.work / "BIG"
    folder.mkdir()
    for n, d in pkgs.items():
        (folder / n).write_bytes(d)
    c = big.server("dev")
    c.add_project("BIG", "BIG", ispac("BIG", pkgs, parameters=["TargetDb"]))
    c.save()
    git(big.work, "add", "-A")
    git(big.work, "commit", "-q", "-m", "BIG project")
    p = big.server("dev").project("BIG", "BIG")
    from ssisbliss.ssis.dtsx import package_sha256
    base.record(folder, base.target_key(SERVERS["dev"], "BIG", "BIG"), p.object_version_lsn,
                {n: package_sha256(d) for n, d in pkgs.items()}, state=big.state_dir)
    (folder / "Load_007.dtsx").write_bytes(task_package("Load_007.dtsx", revision=2, sql=[SQL]))   # one change
    return {"root": root, "big_pkg": big_pkg, "busy": busy.root, "big": big.root}


def _shim(dir_: Path, log: Path) -> None:
    """A git on PATH that logs each call and runs the real git."""
    real = shutil.which("git")
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "git").write_text(f'#!/bin/sh\necho x >> "{log}"\nexec "{real}" "$@"\n')
    (dir_ / "git").chmod(0o755)


def _run(world_root: Path | None, args: list[str], shim: Path | None = None) -> float:
    path = f"{BIN}:/usr/bin:/bin:/usr/local/bin"
    if shim:
        path = f"{shim}:{path}"
    env = {"PATH": path, "HOME": str(world_root or REPO), "SSISBLISS_FAKE_USER": ME}
    cwd = REPO
    if world_root:
        env.update(SSISBLISS_FAKE_ROOT=str(world_root / "servers"), SSISBLISS_STATE_DIR=str(world_root / "state"))
        cwd = world_root / "work"
    t = time.perf_counter()
    subprocess.run([str(BIN / "ssisbliss"), *args], cwd=cwd, env=env, capture_output=True)
    return time.perf_counter() - t


def benchmarks(w: dict[str, Path]) -> dict[str, tuple[Path | None, list[str], bool]]:
    dev = ["--env", "dev", "--folder", "STAGE", "--project", "STAGE"]
    big = ["--env", "dev", "--folder", "BIG", "--project", "BIG"]
    return {   # name: (world, arguments, count git processes)
        "startup": (None, ["--help"], False),
        "lint_big_package": (None, ["lint", str(w["big_pkg"]), "--fail-on", "never"], False),
        "format_check_big_package": (None, ["format", "--check", str(w["big_pkg"])], False),
        "status_200_packages": (w["big"], ["status", *big, "--dir", "BIG"], False),
        "deploy_plan_200_packages": (w["big"], ["deploy-packages", *big, "BIG/Load_007.dtsx"], False),
        "deps_200_packages": (w["big"], ["deps", "BIG/Load_007.dtsx"], False),
        "deps_200_packages_deployed": (w["big"], ["deps", "Load_007", *big], False),
        "activity_40_branches": (w["busy"], ["activity", *dev, "--dir", "STAGE", "Stage_Customer"], True),
        "activity_all_40_branches": (w["busy"], ["activity", *dev, "--dir", "STAGE"], True),
        "strays_40_branches": (w["busy"], ["strays", *dev, "--dir", "STAGE"], True),
    }


def run(out: Path, repeat: int = 5) -> dict:
    with tempfile.TemporaryDirectory(prefix="ssisbliss-perf-") as tmp:
        worlds = build(Path(tmp))
        result = {}
        for name, (world, args, count) in benchmarks(worlds).items():
            _run(world, args)                                   # warm up (file cache, .pyc)
            times = [_run(world, args) for _ in range(repeat)]
            entry: dict = {"seconds": round(statistics.median(times), 3)}
            if count:
                log = Path(tmp) / f"git-{name}.log"
                _shim(Path(tmp) / "shim", log)
                _run(world, args, Path(tmp) / "shim")
                entry["git_processes"] = len(log.read_text().splitlines()) if log.exists() else 0
            result[name] = entry
    out.mkdir(parents=True, exist_ok=True)
    (out / "perf.json").write_text(json.dumps(result, indent=1))
    (out / "perf.md").write_text(markdown(result))
    return result


def markdown(result: dict, best: dict | None = None) -> str:
    lines = ["| benchmark | seconds | git processes |" + (" best seconds |" if best else ""),
             "|---|---|---|" + ("---|" if best else "")]
    for k, v in result.items():
        row = f"| {k} | {v['seconds']} | {v.get('git_processes', '')} |"
        if best:
            row += f" {best.get(k, {}).get('seconds', '')} |"
        lines.append(row)
    return "\n".join(lines) + "\n"


def compare(new: dict, best: dict) -> tuple[list[str], list[str]]:
    """(regressions, improvements) of new against best, each as readable lines."""
    worse, better = [], []
    for k, b in best.items():
        n = new.get(k)
        if not n:
            continue
        if n["seconds"] > b["seconds"] * SLOWER and n["seconds"] - b["seconds"] > SLACK_S:
            worse.append(f"{k}: {b['seconds']}s -> {n['seconds']}s")
        elif n["seconds"] < b["seconds"] * FASTER and b["seconds"] - n["seconds"] > SLACK_S:
            better.append(f"{k}: {b['seconds']}s -> {n['seconds']}s")
        if "git_processes" in b and n.get("git_processes", 0) > b["git_processes"] * (1 + GIT_SLACK) + 2:
            worse.append(f"{k}: {b['git_processes']} -> {n['git_processes']} git processes")
        elif "git_processes" in b and n.get("git_processes", 0) < b["git_processes"] * FASTER:
            better.append(f"{k}: {b['git_processes']} -> {n['git_processes']} git processes")
    return worse, better


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=str(CLIMB / "runs" / f"perf-{time.strftime('%Y%m%d-%H%M%S')}"))
    p.add_argument("--repeat", type=int, default=5)
    a = p.parse_args()
    result = run(Path(a.out), a.repeat)
    print(markdown(result))
    print(a.out)


if __name__ == "__main__":
    os.environ.pop("CLAUDECODE", None)
    main()
