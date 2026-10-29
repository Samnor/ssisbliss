#!/usr/bin/env python3
"""Run the scenario suites with several field agents and compare them per scenario.

The tools should work for small models too: a scenario that Sonnet solves but Haiku or GPT Luna
does not usually means a message, check or help text that only a strong model can work around.

    uv run python climb/matrix.py [--agents claude:sonnet,claude:haiku,codex:gpt-6-luna]
                                  [--suite deploy|troubleshoot|vs] [--repeats 1] [--jobs 4]
Writes climb/runs/matrix-<timestamp>/<agent>/ (as run.py) and matrix.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

CLIMB = Path(__file__).resolve().parent
sys.path.insert(0, str(CLIMB))

from run import run_suite  # noqa: E402

DEFAULT_AGENTS = "claude:sonnet,claude:haiku,codex:gpt-6-luna"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agents", default=DEFAULT_AGENTS)
    p.add_argument("--suite", choices=["deploy", "troubleshoot", "vs", "team", "hard", "lint"])
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--budget", type=float, default=1.0)
    a = p.parse_args()
    root = CLIMB / "runs" / f"matrix-{time.strftime('%Y%m%d-%H%M%S')}"
    agents = [x.strip() for x in a.agents.split(",") if x.strip()]
    cards = {}
    for agent in agents:
        print(f"== {agent}", flush=True)
        cards[agent] = run_suite(root / agent.replace(":", "-"), repeats=a.repeats, model=agent, jobs=a.jobs,
                                 budget=a.budget, suite=a.suite)
    (root / "matrix.json").write_text(json.dumps(cards, indent=2))
    (root / "matrix.md").write_text(markdown(cards, a.repeats))
    print((root / "matrix.md").read_text())
    print(root)


def markdown(cards: dict, repeats: int) -> str:
    agents = list(cards)
    scenarios = sorted({k.rsplit("-", 1)[0] for c in cards.values() for k in c["by_scenario"]})
    lines = [f"# Field agents compared ({repeats} run(s) per scenario)", "",
             "| agent | score | outcomes | safety violations | steps | cost |", "|---|---|---|---|---|---|"]
    for ag, c in cards.items():
        lines.append(f"| {ag} | {c['total']} / {c['max']} | {c['outcomes']}/{c['runs']} | {c['safety_violations']} | "
                     f"{c['steps']} | ${c['cost_usd']} |")
    lines += ["", "Per scenario: right outcomes / runs, safety violations (!), average steps.", "",
              "| scenario | " + " | ".join(agents) + " |", "|---|" + "---|" * len(agents)]
    for s in scenarios:
        cells = []
        for ag in agents:
            runs = [v for k, v in cards[ag]["by_scenario"].items() if k.rsplit("-", 1)[0] == s]
            ok = sum(r["outcome"] for r in runs)
            bad = sum(len(r["safety"]) for r in runs)
            steps = sum(r["steps"] for r in runs) / max(1, len(runs))
            cells.append(f"{ok}/{len(runs)}{' !' * bad} · {steps:.0f}")
        lines.append(f"| {s} | " + " | ".join(cells) + " |")
    lines += ["", "Codex runs report tokens, not dollars (cost $0 here); see each run's grade.json."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
