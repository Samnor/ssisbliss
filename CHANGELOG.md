# Changelog

## Unreleased

- `ssisbliss deps PACKAGE`: how packages connect, from one. The packages it depends on and that depend on
  it (Execute Package calls, tables one writes and another reads in Execute SQL tasks and data flows),
  followed link by link through the project, from your files or the deployed project (`--env`). JSON with
  every link and why, a run `order` (or the `cycles`), and the graph as Mermaid or Graphviz (`--format`).
  SQL built at run time is marked, not guessed. Links are indexed by table: 2000 packages graph in well
  under a second. `lab verify` checks it on a project saved by the SSIS object model. See
  [docs/deps.md](docs/deps.md).
- Tables are matched with their database: the one the SQL names, else the one the connection manager points
  at (its connection string, an expression on it from parameter values, or the catalog's value, including a
  parameter bound to an SSIS environment). Every medallion layer's `dbo.Customer` is now its own table, in
  `deps` and in `activity`; a link through a database that cannot be worked out is marked unsure.
  `deps` reads several projects (`--dir` per folder, or `--also` / `--all-projects` with `--env`).
- `deps` is harder to misread, for people and for small models. Read from your files, a link through a
  database that comes from a parameter's design value (or a project connection manager) is unsure unless both
  packages take it from the same parameter of the same project: the catalog can set other values. The output
  starts with a one-line `summary` that keeps packages reached only through unsure links apart, and `next`
  gives the commands that settle it: the project folders next to the one read, and the deployed projects
  for each environment. A misspelled package gets the command with the right name, a name in several
  projects one command per project, and a folder without packages the folders that have them. The climb has
  a `deps` suite (three medallion layers whose files all say one development database).
- One package model (`ssisbliss/ssis/model.py`): a package is read once into its connection managers (each
  with the database it points at and where that comes from), its tasks in run order and its data flow
  components, and `outline`, `lint`, the design rules, the data flow column checks, `format` and `deps` all
  read it. Their output is unchanged, except that `outline` now shows each connection's `database`, for a
  file from the project's parameters and for a deployed package from the catalog and its environments.
  Tasks are put in run order in linear time: `lint` on a package with 2000 statements takes half the time.
- `executions`: a failing task says which `database` its connection pointed at in that run (a failing data
  flow component, its own connection's), worked out from the run's parameter values, overrides included.
- `pytest` is the quick loop, about 30 seconds: the model checker and the climb's scenarios are marked slow, and
  `pytest -m "slow or not slow"` runs everything, as CI does.
- `AGENTS.md` (and `CLAUDE.md`): a map of the code and how to make the usual changes. On small features, agents
  took 22% to 31% fewer steps with it.
- The package has no import cycles and no process-wide state: an `Invocation` travels with each command.
- `lint --rules` reads no packages: it used to lint everything below the current folder first, and exit 2 when
  those packages had errors.
- A mistyped command suggests the commands that start with it first (`deploy`: `deploy-packages`,
  `deploy-project`).
- A rollback plan says when it undoes only your own deploys; a build refused for its protection level
  points at `status`, so the developer also hears what the server holds.
- `formal/model_check.py`: bounded model checking of the core promises (no lost work, plan binding,
  refusals change nothing, deploys and rollbacks of your own work go through) on every action sequence of
  two developers and a Visual Studio colleague up to a depth, against the real commands; CI runs depth 4.
  See [docs/verification.md](docs/verification.md).
- A deploy after rolling back your own deploy is no longer refused as if someone else had deployed: the
  project version you restored yourself, from before your version, is what your rollback put back.
  Found by the model checker at depth 5; a rollback by someone else still blocks.

## 0.2.0 (2026-11-01)

Changes that scripts may notice: a usage error (unknown command or option, missing argument) now exits 5
with a JSON document, where it was argparse text with exit 2, the same code as a refused plan. Every JSON
document gains `ok` first, `error_kind` when it is false, and `next` when there is a next step; no existing
field changed or moved out.

- `lab verify` drives `ssisbliss mcp` as an MCP client against the real SQL Server: plan, an apply without
  the plan id (refused), the apply, status after it, a read-only chain.
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
