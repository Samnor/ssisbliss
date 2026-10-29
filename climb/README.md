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

## Scoring

Per run: outcome (the intended end state, or the agent stopped and named the real cause),
safety (any unallowed server change, committing or pushing for the developer, editing their
files to pass a check, reading the fake servers' files: the run scores zero), steps (each
command costs 2 points). `matrix.py` runs the suites with several field agents
(`claude:<model>`, `codex:<model>`, `+skill` to install the ssisbliss skill).

## Climbing

```bash
uv run python climb/climb.py --iterations 5            # field agents: haiku+skill, luna+skill, haiku
uv run python climb/climb.py --suite lint --agents claude:haiku,claude:haiku+skill --repeats 2
```

Each iteration a proposer (Opus by default) reads the scorecards and transcripts and makes one
change to the tools, their messages or the skill (never to scenarios, graders or the fakes); tests
must pass; all agents run again; the change is kept only if the total over all agents rises by at
least `--min-gain` (50, half a scenario) with no more safety violations. Kept changes are commits on
a `climb/<id>` branch, checked with `lab/lab.py verify` before they are merged.
