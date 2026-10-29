#!/usr/bin/env python3
"""Hill climb the SSIS deploy tools.

Each iteration: an agent reads the latest scorecard and transcripts and makes ONE focused
change to the tools; tests must pass; the suite is re-run; the change is committed if the
score went up with no more safety violations, otherwise reverted. Runs on a climb/ branch.

    uv run python climb/climb.py --iterations 3 [--repeats 2] [--model sonnet] [--proposer opus]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CLIMB = Path(__file__).resolve().parent
REPO = CLIMB.parent
sys.path.insert(0, str(CLIMB))

from run import run_suite  # noqa: E402

# What the proposer may change. The scenarios, graders, runner and the fake catalog
# (which the graders read) are off limits, so the score can only move by changing the tools.
ALLOWED = ("src/ssisbliss/ssis/", "src/ssisbliss/agent/", "docs/", "tests/test_ssis_deploy.py",
           "tests/test_agent.py", "tests/test_outline_executions.py", "tests/test_build.py", "README.md")
FORBIDDEN = ("src/ssisbliss/ssis/fake.py", "src/ssisbliss/agent/fake.py")

PROPOSE = """You are improving the ssisbliss CLIs used by developers and by LLM agents in the field:
`ssisbliss` (safe SSIS catalog deployments; `executions` and `outline` explain failed packages) and
`ssisbliss` (SQL Agent jobs, job chains, run history). A suite of field scenarios was just run: an agent got a
developer's request and used the CLIs against fake dev/stage/prod servers. Results:

{scorecard}

Per-run details (grade.json has the agent's commands and final message; transcript.jsonl the full session) are in:
{run_dir}

Make ONE focused change to the tool that would make agents reach the right outcome more often, in fewer steps,
or with clearer explanations to the developer. Good targets: confusing or missing guidance in error messages and
check details, a missing option that forced a workaround, help text, output that is hard to act on.

Rules:
- Only edit files under src/ssisbliss/ssis/ and src/ssisbliss/agent/ (not fake.py in either), docs/,
  tests/test_ssis_deploy.py, tests/test_agent.py, tests/test_outline_executions.py, tests/test_build.py.
- The troubleshooting tools are read-only; keep them that way.
- Never weaken a safety check: refusing to overwrite others' work, prod rules, plan_id, running-execution and
  protection-level checks must stay at least as strict.
- Add or update tests for the change; `uv run pytest -q` must pass.
- When done, write a short description of the change (first line = summary) to {summary_file}."""


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=check, capture_output=True, text=True).stdout


def changed_files() -> list[str]:
    out = git("status", "--porcelain", "--untracked-files=all")
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def revert() -> None:
    git("checkout", "--", ".")
    for f in changed_files():          # untracked files the proposer created
        p = REPO / f
        if p.is_file() and f.startswith(ALLOWED):
            p.unlink()


def tests_pass() -> bool:
    return subprocess.run(["uv", "run", "pytest", "-q"], cwd=REPO, capture_output=True).returncode == 0


def better(new: dict, best: dict) -> bool:
    return new["safety_violations"] <= best["safety_violations"] and new["total"] > best["total"]


def propose(run_dir: Path, card_md: str, summary_file: Path, model: str, budget: float) -> None:
    env = {**os.environ}
    env.pop("CLAUDECODE", None)
    prompt = PROPOSE.format(scorecard=card_md, run_dir=run_dir, summary_file=summary_file)
    subprocess.run(["claude", "-p", prompt, "--model", model, "--permission-mode", "acceptEdits",
                    "--setting-sources", "project", "--max-budget-usd", str(budget),
                    "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep",
                    "Bash(uv run pytest:*)", "Bash(git diff:*)", "Bash(git status:*)", "Bash(cat:*)", "Bash(ls:*)"],
                   cwd=REPO, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=1800)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--iterations", type=int, default=3)
    p.add_argument("--repeats", type=int, default=2, help="runs per scenario per evaluation (noise control)")
    p.add_argument("--model", default="sonnet", help="model playing the field agent")
    p.add_argument("--proposer", default="opus", help="model proposing tool changes")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--budget", type=float, default=1.0, help="max USD per scenario run")
    p.add_argument("--baseline", help="existing run dir to use as the baseline")
    p.add_argument("--suite", choices=["deploy", "troubleshoot", "vs", "team", "hard", "lint"], help="only this scenario suite")
    a = p.parse_args()

    if changed_files():
        sys.exit("working tree not clean; commit or stash first")
    climb_id = time.strftime("%Y%m%d-%H%M%S")
    root = CLIMB / "runs" / f"climb-{climb_id}"
    root.mkdir(parents=True)
    git("checkout", "-b", f"climb/{climb_id}")
    log = root / "log.md"

    def note(text: str) -> None:
        print(text, flush=True)
        with open(log, "a") as f:
            f.write(text + "\n")

    if a.baseline:
        best_dir = Path(a.baseline)
        best = json.loads((best_dir / "scorecard.json").read_text())
    else:
        best_dir = root / "iter-0"
        best = run_suite(best_dir, repeats=a.repeats, model=a.model, jobs=a.jobs, budget=a.budget, suite=a.suite)
    note(f"# Climb {climb_id}\n\nbaseline: {best['total']} (outcomes {best['outcomes']}/{best['runs']}, "
         f"safety {best['safety_violations']}, cost ${best['cost_usd']})\n")

    for i in range(1, a.iterations + 1):
        summary_file = root / f"iter-{i}-proposal.md"
        propose(best_dir, (best_dir / "scorecard.md").read_text(), summary_file, a.proposer, budget=5.0)
        files = changed_files()
        summary = summary_file.read_text().strip() if summary_file.exists() else "(no summary)"
        title = summary.splitlines()[0][:72] if summary else "(no summary)"
        bad = [f for f in files if not f.startswith(ALLOWED) or f.startswith(FORBIDDEN)]
        if not files:
            note(f"## iter {i}: no change proposed")
            continue
        if bad:
            note(f"## iter {i}: reverted, touched files outside the allowed set: {', '.join(bad)}")
            revert()
            continue
        if not tests_pass():
            note(f"## iter {i}: reverted, tests fail — {title}")
            revert()
            continue
        run_dir = root / f"iter-{i}"
        card = run_suite(run_dir, repeats=a.repeats, model=a.model, jobs=a.jobs, budget=a.budget, suite=a.suite)
        verdict = "kept" if better(card, best) else "reverted"
        note(f"## iter {i}: {verdict} — {title}\n\nscore {best['total']} -> {card['total']}, "
             f"outcomes {card['outcomes']}/{card['runs']}, safety {card['safety_violations']}, "
             f"cost ${card['cost_usd']}\nfiles: {', '.join(files)}\n")
        if verdict == "kept":
            git("add", "-A")
            git("commit", "-q", "-m", f"climb: {title}\n\n{summary}\n\nScore {best['total']} -> {card['total']} "
                f"({climb_id} iter {i})\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
            best, best_dir = card, run_dir
        else:
            revert()

    note(f"\nfinal: {best['total']} on branch climb/{climb_id}")


if __name__ == "__main__":
    main()
