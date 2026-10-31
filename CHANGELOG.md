# Changelog

## Unreleased

- One output contract for every command, for agents and scripts ([docs/agents.md](docs/agents.md)): `ok`
  (true exactly when the exit code is 0), `error_kind` when it is false, and `next`: what to run next,
  ready to run with the same `--env` or `--server` (a plan's apply command, with its `plan_id` and
  `apply_when`). Usage errors are JSON too, exit 5, with the closest commands. Before, they were
  argparse text with exit 2, the same code as a refused plan.
- `ssisbliss guide` (the skill's rules and recipes) and `ssisbliss commands` (every command and option
  as JSON, and the exit codes), for agents that have no skill installed.
- `ssisbliss mcp` serves every command as an MCP tool over stdio, with schemas built from the commands'
  options and the same safety: nothing changes without `apply` and the plan's `plan_id`.
- The commands moved out of `cli.py` into `ssisbliss/ssis/commands/`; `ssisbliss.ssis.cli.run(argv)`
  returns a command's answer without printing it.
- `--folder` and `--project` may be left out, unlike Visual Studio's deploy wizard, which asks every
  time: the project is the `.dtproj`'s name, and the folder is where you last deployed or pulled that
  project on that server, else the environment's `folder` in `ssisbliss.toml`. The output says which
  ones were chosen and why (`target_from`). The server is still always named.
- `executions` explains errors SSIS logs without a cause (`explained`): the real error inside
  `0xC0202009`, VS_NEEDSNEWMETADATA, truncation, the 64-bit Excel driver, logins and permissions
  with the run's account, mapped drives, no environment in the job step, script tasks built for
  another SQL Server, deadlocks, timeouts and more, each with the usual fix.
- `lint --env` compares every data flow source and destination with the server's tables (SSB401-405):
  a dropped or retyped column, a column that truncates, a new required column in the destination.
- `jobs --check` finds job steps that will fail on their next run: a package renamed or removed by a
  deploy, a `/Par` override for a parameter that is gone, an environment reference the project does
  not have, a jump to a missing step.
- `health`: catalog retention and cleanup, SSISDB size and log, the default logging level, executions
  stuck as running after a restart; each warning with the T-SQL fix.
- `slow` names the tasks that made a run slower than usual: each task's time against its median over
  earlier successful runs, with row counts when the runs logged them. `history` marks a step that took
  much longer than in the other runs listed and points at `slow`.
- New lint rules for speed and for packages that work in Visual Studio but fail under SQL Agent:
  row-by-row components (SSB501), destinations without fast load (SSB502), lookups without a cache
  (SSB503), Sort (SSB504), Excel through Jet or ACE (SSB510; with `--env`, a provider the server does
  not have is SSB511), temp tables on a connection that does not keep its session (SSB520), a server
  name written in a package's connection string (SSB521).

## 0.1.0 (2026-10-30)

First release.

- `ssisbliss`: plan-then-apply SSIS catalog deploys of single packages, project subsets,
  parameters and connection managers that never overwrite other people's work; `status`,
  `pull`, `remove-packages`, `rollback` (refused when it would undo others' deploys),
  `deploy-packages --from-ref`; per-user records that follow the repository across branches
  and worktrees; prod rules (committed, pushed, confirmed).
- `ssisbliss build`: Visual Studio SSIS projects to `.ispac` without Visual Studio.
- `ssisbliss executions` / `outline`, `ssisbliss jobs` / `chain` / `history`: why a
  nightly load or package failed, read-only.
- `ssisbliss activity`: who else is working on a package or on one linked to it (git branches,
  the server's copy, runs by people, Execute Package calls and tables); deploy plans warn about it.
- `ssisbliss strays` and `remove-packages --strays`: find and safely remove deployed packages nobody has.
  The git scan behind them reads every package version through one `git cat-file --batch` process
  (about 4× faster with 40 colleague branches).
- Base records are read once per folder rather than once per package: `status` and deploy plans on a
  200-package project take well under a second (were 3.5 s and 7 s).
- `ssisbliss lint`, `format` and `sql-diff`: lint packages and their SQL (correctness and readability,
  optional server parsing, baselines, `--changed`); lay out SQL readably and safely; show SQL changes
  as a readable diff for pull requests.
- `ssisbliss install-skill`: the ssisbliss skill for Claude Code and Codex.
