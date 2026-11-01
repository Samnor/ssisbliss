# Hill climb: improving the tools against field scenarios

Goal: make the tools in this repo better for the person (or agent) using them in the
field, one measured change at a time. "Better" means: the right outcome more often,
dangerous outcomes never, in fewer steps, with errors that tell you what to do next.

## Loop

```
scenarios ──► run agent with the current tools ──► score ──► compare with best
    ▲                                                           │
    └──── keep the change if score up and no safety regression ◄┘
                    ▲
        propose ONE change to a tool (code, messages, docs, CLI shape)
```

1. **Baseline**: run every scenario, store the scorecard (`climb/runs/<id>/`).
2. **Propose**: an agent reads the failing / clumsy transcripts and makes one focused
   change to the tools (not to the scenarios or graders).
3. **Re-run and judge**: same scenarios. Keep the change only if the total score rises and
   no safety check gets worse. Otherwise revert.
4. Repeat for N iterations; each kept change is a commit with its scorecard.

## Scenarios

Scenarios are Python (`climb/scenarios.py` and the builders it imports): each builds a world with
fake dev/stage/prod SSIS catalogs and SQL Agent, a git repository with a team remote (colleagues'
branches, worktrees), the developer's working folder, and a developer's request; a deterministic
grader scores the end state and the agent's final message. Suites:

| Suite | What it covers |
|---|---|
| `deploy` | deploying packages and projects safely: colleagues' changes, prod rules, races, rollback |
| `troubleshoot` | why the nightly load failed: job chains, history, executions, parameter overrides |
| `vs` | Visual Studio projects: rebuild and deploy package, full deploys, user-key protection |
| `team` | PR branches on a shared dev: worktrees, reset to main, new parameters, who else works on a package, strays |
| `hard` | requests that name less and traps that need several commands |
| `lint` | checking a package before a PR, laying out its SQL, the SQL changes for a PR description |
| `deps` | how packages connect across medallion layers whose files all name one development database |

## Scoring

Per run: outcome (the intended end state, or the agent stopped and named the real cause),
safety (any unallowed server change, committing or pushing for the developer, editing their
files to pass a check, reading the fake servers' files: the run scores zero), steps (each
command costs 2 points). `matrix.py` runs the suites with several field agents
(`claude:<model>`, `codex:<model>`, `+skill` to install the ssisbliss skill).

## Speed and scale

`climb/perf.py` runs deterministic benchmarks (no agents) at the sizes real estates reach: startup; lint
and `format --check` on a 1.2 MB package with 2000 SQL statements; `status` and a deploy plan on a
project with 200 packages; `activity` and `strays` in a repository with 40 colleague branches. Each is
the median of several runs; for the git-heavy commands it also counts git processes, which does not
move with the machine's load. First measurement (2026-10-30, this Mac):

| benchmark | seconds | git processes |
|---|---|---|
| startup | 0.03 | |
| lint, 1.2 MB package | 1.1 | |
| format --check, 1.2 MB package | 0.4 | |
| status, 200 packages | 3.5 | |
| deploy plan, 200 packages | 7.2 | |
| activity for one package, 40 branches | 5.2 | 563 |
| activity for all packages, 40 branches | 8.2 | 824 |
| strays, 40 branches | 4.7 | 504 |

## Climbing

```bash
uv run python climb/climb.py --iterations 5            # field agents: haiku+skill, luna+skill, haiku
uv run python climb/climb.py --suite lint --agents claude:haiku,claude:haiku+skill --repeats 2
```

Each iteration a proposer (Opus by default) reads the scorecards and transcripts and makes one
change to the tools, their messages or the skill (never to scenarios, graders or the fakes); tests
must pass; all agents run again; the change is kept only if the total over all agents rises by at
least `--min-gain` (50, half a scenario) with no more safety violations. Speed is a gate and a goal:
the benchmarks run before the agents, and a change that makes one clearly slower (25% and 0.15 s, or
10% more git processes) is reverted at once; a change that makes one clearly faster (20%) is kept when
the agents scored no worse than noise (two `--min-gain`). A clearly faster change that scores up to six
`--min-gain` below the best is run once more and judged on the average of the two runs, since one run
moves by about 200 points on noise alone. A clearly faster change is first replayed (`climb/sameoutput.py`): every `ssisbliss` command the agents
ran in the best run, and every `--help` page, on fresh scenario worlds with the old and the new code. If
all output is identical, the agents cannot have scored differently, so it is kept on speed alone, without
an agent run (which would only add noise). A change to the skill always goes to the agents.
Every proposal's diff is saved as `iter-N.patch` in the run
folder, kept or not. Kept changes are commits on
a `climb/<id>` branch, checked with `lab/lab.py verify` before they are merged.
