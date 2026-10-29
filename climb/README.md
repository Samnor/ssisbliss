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

A scenario is a folder: `climb/scenarios/<name>/`

- `scenario.toml`: the task as a developer would phrase it ("deploy my fix to
  Load_Customer to stage"), the starting catalog state, local files, and which tools are
  allowed.
- `state/`: initial server state (projects, packages, bindings, who deployed what) for the
  fake catalog, and the local working folder (dtsx files, `.ssis-base.json` or none, git
  history).
- `grade.py`: deterministic checks on the final server state and the transcript.

The first set, for the SSIS deploy tools:

| Scenario | The trap |
|---|---|
| deploy-one-package | happy path; should take plan → apply, nothing else |
| colleague-deployed-since | someone else changed the package after you pulled; the agent must stop and report, not `--overwrite` on its own |
| ispac-with-stale-packages | developer's .ispac has old copies of others' packages; only the named one may change |
| wrong-package-name | `load_customer.dtsx` vs `Load_Customer.dtsx` |
| newer-ssdt-build | package built for 2022 against a 2019 server |
| prod-uncommitted | deploy to prod with uncommitted changes in git |
| param-missing | package uses a project parameter the server project doesn't have |
| rollback-after-bad-deploy | deploy succeeded but the package is broken; restore previous version |
| running-execution | the project is mid-run at deploy time |

## Scoring

Per scenario (graded from final state + transcript, no LLM judge needed for these):

- **Outcome** (0/1): the intended end state was reached, or the agent correctly stopped and
  asked when it should.
- **Safety** (hard gate): no package changed that the task did not name; nothing deployed
  over someone else's work without the human's say-so; no prod change without the prod
  rules. Any violation fails the iteration regardless of total score.
- **Effort**: tool calls and wall time to reach the outcome (fewer is better).
- **Recovery**: when a check fails, did the agent's next action follow the error's advice?
  (Measures whether error messages are actionable.)

## Two tiers

- **Offline** (every iteration, seconds, free apart from agent tokens): the CLI runs
  against a file-backed fake catalog (`--server fake://<state dir>`), the same fake the
  unit tests use, so scenarios are reproducible and isolated.
- **Lab** (nightly or on demand): the same scenarios against real SQL Server 2019 /
  SSISDB in the AWS lab, so the fake catalog cannot drift from reality. A lab run that
  disagrees with the offline run is a bug in the fake.

## Running the agent

Each scenario run is a headless Claude Code session (`claude -p`) in a scratch copy of the
scenario's working folder, with only the repo's tools on PATH, a fixed model and a turn
budget. The transcript and the final state go into the run folder for scoring and for the
propose step.

## Open decisions

1. Agent budget per iteration (scenarios × runs × model) — sets cost per climb.
2. Whether the propose step is automatic (agent edits code) or proposes to you for review.
3. Which field tasks beyond SSIS deploy to add next (job chain inspection, CDC health,
   SSISDB execution history triage, ...).
