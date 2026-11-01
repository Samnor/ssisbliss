#!/usr/bin/env python3
"""Hill climb ssisbliss: the tools, their messages and the ssisbliss skill.

Each iteration: an agent reads the latest scorecards and transcripts and makes ONE focused change to
the tools or the skill; tests must pass; every scenario is run again with every field agent; the change
is kept (committed) if the average score per run rose by at least --min-gain with no more safety
violations, otherwise reverted. Speed and scale are measured too (climb/perf.py: deterministic
benchmarks at real sizes): a change that makes a benchmark clearly slower is reverted whatever the
agents scored, and a change that makes one clearly faster is kept if the agents did not get worse
(a faster change that scores just below that is run once more and judged on the average of both runs).
A change that is clearly faster and leaves the output of every command the agents ran unchanged
(climb/sameoutput.py) is kept without agent runs: the agents cannot have scored differently.
Every proposal's diff is saved as iter-N.patch, kept or not. Runs on a climb/ branch.

The field agents default to the small models the tools are built for, each with the skill and over MCP:
claude:haiku+skill, codex:gpt-6-luna+skill, claude:haiku+mcp and codex:gpt-6-luna+mcp, all at once. A change
has to help them together. Each scenario runs twice per agent by default: one run moves a total by about as much
as a typical change does.

    uv run python climb/climb.py --iterations 5 [--agents ...] [--repeats 2] [--suite lint] [--proposer opus]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CLIMB = Path(__file__).resolve().parent
REPO = CLIMB.parent
sys.path.insert(0, str(CLIMB))

import perf  # noqa: E402
import sameoutput  # noqa: E402
from matrix import markdown as matrix_markdown  # noqa: E402
from run import run_suite  # noqa: E402

# What the proposer may change. The scenarios, graders, runner and the fake catalog and SQL Agent
# (which the graders read) are off limits, so the score can only move by changing the tools or the skill.
ALLOWED = ("src/ssisbliss/", "docs/", "README.md", "tests/test_ssis_deploy.py", "tests/test_agent.py",
           "tests/test_outline_executions.py", "tests/test_build.py", "tests/test_activity.py", "tests/test_lint.py",
           "tests/test_sqlformat.py", "tests/test_contract.py")
FORBIDDEN = ("src/ssisbliss/ssis/fake.py", "src/ssisbliss/agent/fake.py")
DEFAULT_AGENTS = "claude:haiku+skill,codex:gpt-6-luna+skill,claude:haiku+mcp,codex:gpt-6-luna+mcp"

PROPOSE = """You are improving ssisbliss, a command-line tool for SSIS packages and SQL Server SSIS catalogs that
developers and LLM agents use, and the ssisbliss skill (src/ssisbliss/skills/ssisbliss/SKILL.md) that teaches
agents to use it. One command, `ssisbliss`, covers:
- deploying safely: deploy-packages, deploy-project, build, status, pull, versions, rollback, remove-packages
  (plan first, apply with the plan's id; never overwrite a colleague's work);
- team work: activity (who else works on a package or a linked one), strays (packages nobody has);
- reviewing: lint, format (SQL laid out readably), sql-diff (SQL changes for a pull request);
- troubleshooting, read-only: jobs, chain, history (SQL Agent), executions, slow, health, outline;
- about itself, for agents without the skill: guide (the skill's rules and recipes), commands (every command and
  option as JSON); `ssisbliss mcp` serves every command as an MCP tool.
Every command keeps one output contract (src/ssisbliss/ssis/contract.py): one JSON document, `ok`, `error_kind`
when it is false, and `next` (what to run next). Keep it; improve what goes into it.

Field scenarios were just run: each field agent (a small model, some with the skill installed, some without,
one through the MCP server instead of the shell)
got a developer's request and used the tool against fake dev/stage/prod servers and a git repository. Results
per agent and scenario:

{scorecard}

Per-run details (grade.json: the agent's commands, final message and grading notes; transcript.jsonl: the full
session) are under {run_dirs} (one folder per agent).

Speed and scale (deterministic benchmarks at real sizes: a 1.2 MB package with 2000 SQL statements, a project with
200 packages, a repository with 40 colleague branches; median seconds and git processes started):

{perf}

Make ONE focused change that would make these agents reach the right outcome more often, in fewer steps, or
explain it better to the developer; or that makes the tool clearly faster at scale (fewer git processes,
less repeated parsing or hashing, fewer server round trips) without changing what it reports. Look for patterns across agents and scenarios, not one lucky run (even two runs per
scenario are noisy). Good targets: an error or check message that agents misread, guidance that is missing where
agents look (command output, --help, the skill), a missing option that forced a workaround, output that is hard
to act on, the skill's description (what makes an agent load it) or its task table.

Rules:
- Only edit files under src/ssisbliss/ (not ssis/fake.py or agent/fake.py), docs/, README.md and the tests
  named in this list: tests/test_ssis_deploy.py, test_agent.py, test_outline_executions.py, test_build.py,
  test_activity.py, test_lint.py, test_sqlformat.py, test_contract.py.
- Never weaken a safety check: refusing to overwrite others' work, prod rules, plan_id, running executions,
  protection level, rollback over others' deploys, stray checks, and format's rule that only whitespace and
  keyword case may change, must stay at least as strict. Troubleshooting and lint stay read-only.
- The skill: name must stay `ssisbliss` (its folder name); the description at most 1024 characters (VS Code and
  other agents skip a skill that breaks this).
- No client, company or personal names anywhere.
- A change that makes any benchmark clearly slower is reverted, whatever the agents scored.
- A change that makes a benchmark clearly faster and leaves every output the agents saw byte-identical is
  kept on speed alone. So a pure speed change should not touch any message, help text or JSON field.
- Add or update tests for the change; `uv run pytest -q` must pass.
- When done, write a short description of the change (first line = summary) to {summary_file}.

Changes already tried, with what happened (do not repeat a reverted idea; if you build on one, say what is
different and why it should do better):

{history}{idea}"""


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=check, capture_output=True, text=True).stdout


def changed_files() -> list[str]:
    out = git("status", "--porcelain", "--untracked-files=all")
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def save_patch(path: Path, files: list[str]) -> None:
    """The proposal as a patch (tracked changes and new files), so a reverted idea can be picked up again."""
    tracked = set(git("ls-files").splitlines())
    parts = [git("diff", "--binary")]
    parts += [git("diff", "--no-index", "--binary", "/dev/null", f, check=False) for f in files if f not in tracked]
    path.write_text("".join(parts))


def revert() -> None:
    git("checkout", "--", ".")
    for f in changed_files():          # untracked files the proposer created
        p = REPO / f
        if p.is_file() and f.startswith(ALLOWED):
            p.unlink()


def tests_pass() -> bool:
    return subprocess.run(["uv", "run", "pytest", "-q"], cwd=REPO, capture_output=True).returncode == 0


def totals(cards: dict[str, dict]) -> dict:
    keys = ("total", "max", "outcomes", "runs", "safety_violations", "steps", "cost_usd")
    return {k: round(sum(c[k] for c in cards.values()), 2) for k in keys}


def measure(out: Path, a) -> dict:
    if (out / "perf.json").exists():
        return json.loads((out / "perf.json").read_text())
    return perf.run(out, repeat=a.perf_repeat)


# A clearly faster change whose agents scored up to this many --min-gain below the best is run once more
# and judged on the average (one run of 144 scenarios moved by about 1.4 points per run on noise alone).
RECHECK = 6


def speed_only(files: list[str]) -> bool:
    """Could the agents only see this change through the tools' output? The skill and its instructions
    reach them directly, so a change to those always goes to the agents."""
    return all(not f.startswith("src/ssisbliss/skills/") for f in files)


def decide(new: dict, best: dict, new_perf: dict, best_perf: dict, min_gain: float) -> tuple[bool, str]:
    """Keep a change? (verdict, reason)."""
    worse, faster = perf.compare(new_perf, best_perf)
    if new["safety_violations"] > best["safety_violations"]:
        return False, "more safety violations"
    if worse:
        return False, "slower: " + "; ".join(worse)
    gain = new["total"] / max(1, new["runs"]) - best["total"] / max(1, best["runs"])   # per run: suites grow
    if gain >= min_gain:
        return True, "agents did better" + (" and faster: " + "; ".join(faster) if faster else "")
    if faster and gain >= -2 * min_gain:
        return True, "faster, agents no worse (within noise): " + "; ".join(faster)
    if faster and gain >= -RECHECK * min_gain:
        return False, "recheck"
    return False, "not enough better"


def average(a: dict, b: dict) -> dict:
    return {k: round((a[k] + b[k]) / 2, 2) for k in a}


def evaluate(out: Path, agents: list[str], a) -> dict[str, dict]:
    # The agents run at the same time (--jobs runs each): an evaluation takes as long as the slowest agent.
    with ThreadPoolExecutor(len(agents)) as pool:
        cards = dict(zip(agents, pool.map(lambda agent: run_suite(
            out / agent.replace(":", "-"), repeats=a.repeats, model=agent, jobs=a.jobs, budget=a.budget,
            suite=a.suite), agents)))
    (out / "matrix.json").write_text(json.dumps(cards, indent=2))
    (out / "matrix.md").write_text(matrix_markdown(cards, a.repeats))
    return cards


def propose(run_dir: Path, card_md: str, summary_file: Path, model: str, budget: float, history: str,
            perf_md: str, idea: str = "") -> None:
    env = {**os.environ}
    env.pop("CLAUDECODE", None)
    prompt = PROPOSE.format(scorecard=card_md, run_dirs=run_dir, summary_file=summary_file,
                            history=history.strip() or "(none yet)", perf=perf_md,
                            idea=f"\n\nTry this idea first (check it against the transcripts; make it the one change): {idea}"
                            if idea else "")
    subprocess.run(["claude", "-p", prompt, "--model", model, "--permission-mode", "acceptEdits",
                    "--setting-sources", "project", "--max-budget-usd", str(budget),
                    "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep",
                    "Bash(uv run pytest:*)", "Bash(git diff:*)", "Bash(git status:*)", "Bash(cat:*)", "Bash(ls:*)"],
                   cwd=REPO, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=1800)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--iterations", type=int, default=3)
    p.add_argument("--agents", default=DEFAULT_AGENTS, help="field agents, comma separated (see matrix.py)")
    p.add_argument("--repeats", type=int, default=2, help="runs per scenario per agent per evaluation")
    p.add_argument("--min-gain", type=float, default=0.35,
                   help="points the average score per run (out of 100) must rise by to keep a change: noise control")
    p.add_argument("--proposer", default="opus", help="model proposing tool changes")
    p.add_argument("--jobs", type=int, default=6, help="runs at a time per agent")
    p.add_argument("--budget", type=float, default=1.0, help="max USD per scenario run")
    p.add_argument("--baseline", help="existing climb evaluation dir (with matrix.json) to use as the baseline")
    p.add_argument("--suite", choices=["deploy", "troubleshoot", "vs", "team", "hard", "lint", "field"], help="only this scenario suite")
    p.add_argument("--perf-repeat", type=int, default=3, help="runs per speed benchmark (median)")
    p.add_argument("--history", action="append", default=[], metavar="LOG",
                   help="log.md of an earlier climb, so the proposer knows what was tried (repeatable)")
    p.add_argument("--idea", default="", help="a change for the first iteration to try")
    a = p.parse_args()
    agents = [x.strip() for x in a.agents.split(",") if x.strip()]

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

    def describe(t: dict) -> str:
        return (f"{t['total']}/{t['max']} (outcomes {t['outcomes']}/{t['runs']}, safety {t['safety_violations']}, "
                f"steps {t['steps']}, cost ${t['cost_usd']})")

    if a.baseline:
        best_dir = Path(a.baseline)
        best_cards = json.loads((best_dir / "matrix.json").read_text())
    else:
        best_dir = root / "iter-0"
        best_cards = evaluate(best_dir, agents, a)
    best = totals(best_cards)
    best_perf = measure(best_dir, a)
    note(f"# Climb {climb_id}\n\nagents: {', '.join(agents)}\nbaseline: {describe(best)}\n\n{perf.markdown(best_perf)}")

    for i in range(1, a.iterations + 1):
        summary_file = root / f"iter-{i}-proposal.md"
        history = "\n".join(Path(h).read_text() for h in a.history) + "\n" + (log.read_text() if log.exists() else "")
        propose(best_dir, (best_dir / "matrix.md").read_text(), summary_file, a.proposer, budget=8.0, history=history,
                perf_md=perf.markdown(best_perf), idea=a.idea if i == 1 else "")
        files = changed_files()
        if files:
            save_patch(root / f"iter-{i}.patch", files)
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
        new_perf = measure(run_dir, a)
        worse, faster = perf.compare(new_perf, best_perf)
        if worse:                                   # no agent runs needed: slower is reverted regardless
            note(f"## iter {i}: reverted, slower — {title}\n\n" + "; ".join(worse) + f"\nfiles: {', '.join(files)}\n")
            revert()
            continue
        if faster and speed_only(files):            # faster and everything agents saw is unchanged: keep, no agent runs
            same = sameoutput.compare(best_dir)
            if same["same"]:
                note(f"## iter {i}: kept (faster, and all {same['commands']} commands the agents ran give the same "
                     f"output) — {title}\n\n" + "; ".join(faster) + f"\nfiles: {', '.join(files)}\n\n"
                     f"{perf.markdown(new_perf, best_perf)}")
                git("add", "-A")
                git("commit", "-q", "-m", f"climb: {title}\n\n{summary}\n\nFaster with the same output for all "
                    f"{same['commands']} agent commands ({climb_id} iter {i}): {'; '.join(faster)}\n\n"
                    "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
                best_perf = new_perf
                continue
            note(f"## iter {i}: faster, but output changed ({'; '.join(same['differences'][:3])}): agents decide")
        cards = evaluate(run_dir, agents, a)
        new = totals(cards)
        keep, why = decide(new, best, new_perf, best_perf, a.min_gain)
        if why == "recheck":                        # faster, scores just below noise: run the agents once more
            again = totals(evaluate(run_dir / "recheck", agents, a))
            note(f"## iter {i}: faster but {describe(new)}; agents run again: {describe(again)}")
            new = average(new, again)
            keep, why = decide(new, best, new_perf, best_perf, a.min_gain)
            why = "on the average of two runs: " + ("not enough better" if why == "recheck" else why)
        verdict = "kept" if keep else "reverted"
        per_agent = "; ".join(f"{ag} {best_cards[ag]['total']}->{cards[ag]['total']}" for ag in agents if ag in best_cards)
        note(f"## iter {i}: {verdict} ({why}) — {title}\n\n{describe(best)} -> {describe(new)}\n{per_agent}\n"
             f"files: {', '.join(files)}\n\n{perf.markdown(new_perf, best_perf)}")
        if verdict == "kept":
            git("add", "-A")
            git("commit", "-q", "-m", f"climb: {title}\n\n{summary}\n\nScore {best['total']} -> {new['total']} "
                f"({climb_id} iter {i}; {per_agent})\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
            best, best_cards, best_dir, best_perf = new, cards, run_dir, new_perf
        else:
            revert()

    note(f"\nfinal: {describe(best)} on branch climb/{climb_id}")


if __name__ == "__main__":
    main()
