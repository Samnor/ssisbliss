# Why did the load fail? (`ssisbliss chain`, `history`, `executions`, `outline`)

Read-only tools for the most common question in the field: the nightly job chain broke,
where and why. Every command prints one JSON document, so a person can read it and an LLM
agent can follow it step by step. Each answer names the command to run next.

```text
ssisbliss chain        -> "failed at 3 STAGE"
ssisbliss history "3 STAGE" --runs 1
                          -> step failed, Execution ID 78, next: ssisbliss executions --id 78
ssisbliss executions --id 78
                          -> failing task, its error, its SQL or command, the parameter values it used
ssisbliss outline --folder STAGE --project STAGE --package Main
                          -> everything the package does, in run order
```

All commands take `--env NAME` (from `ssisbliss.toml`, see [ssis-deploy.md](ssis-deploy.md))
or `--server`, and use Windows authentication by default.

## SQL Agent: `ssisbliss jobs`, `chain`, `history`

| Command | Shows |
|---|---|
| `jobs [--name X] [--failed] [--running] [--brief]` | each job: enabled, running now, last run (outcome, duration, message), schedules, next run, and per step what it runs: the SSIS catalog package with its `/Par` overrides and environment reference, a file or msdb package, or the T-SQL; its `on_success` / `on_failure` action (`go to next step`, `quit reporting success`, ...); which jobs it starts and which jobs start it. `skipped_starts` (kept in `--brief`) lists steps that start a job but never run when the job succeeds, because an earlier step quits on success |
| `chain [--name X]` | job chains linked by `sp_start_job` steps, in order, each link's state in the current or last run (`succeeded`, `failed`, `running`, `not_started`) and a verdict: `completed`, `running: X`, `failed at X`, `stopped after X: Y did not start`. When a chain stopped because X's start-next step is skipped on success, `why` says which step quits the job. When a link is still running, `why` says nothing has failed (no error exists yet; `history` lists only finished runs) and gives its last run's duration (`last_run_took_s`) so an overrun shows. Steps that start a job that does not exist are listed as `missing_jobs` |
| `history JOB [--runs N] [--failed]` | recent runs with every step's outcome and message; SSIS steps get their `ssis_execution_id`, failed ones the `ssisbliss executions --id` command; a step whose package was not found (renamed or removed by a deploy, so no execution exists) gets a `hint` and an `ssisbliss inspect` command |

The chain is built from the jobs themselves (`sp_start_job` in step commands), not from
naming conventions, so it works for numbered chains (`1 IMPORT`, `2 STAGE`, ...) and any
other naming. A link counts as part of the current run if it started after the first job
did.

## `ssisbliss executions`

```bash
ssisbliss executions --env prod --failed --since-hours 24
ssisbliss executions --env prod --id 78
ssisbliss executions --env dev --folder STAGE --project STAGE --limit 5
```

For each failed (or unexpectedly ended) execution:

- **failed_tasks**: each task that logged an error or that SSISDB recorded as failed (the
  latter is kept even at logging level None/Basic), with its error messages (the generic
  "The task, 'X', failed." is dropped when the real error is there).
- **What the task runs**, from the deployed package: for an Execute SQL task the statement
  and connection; for an Execute Process task the executable, arguments, working directory,
  and whether it runs PowerShell or Python and which script.
- **connection_parameters**: the values of the project/package parameters in the expression
  of the connection the failing task uses, as that run saw them, marked `(overridden for
  this run)` when a job step or caller set them with `/Par`. A job that points `TargetDb`
  at a read-only history database shows up right here. Sensitive values are never read.
- **override_note**: when one of those parameters was overridden, says that the value came
  from the caller (e.g. the SQL Agent job step's `/Par "$Project::TargetDb";...`), not from
  the project or environment, and points at `ssisbliss jobs` to see it. The fix belongs
  in the job step (or the database it points to); redeploying the package won't change it.
- **hint**: for PowerShell/Python/other process tasks, SSIS logs only the exit code, not
  the process's own error output (unless the task has a `StandardErrorVariable`). The hint
  gives the exact command line and machine to rerun it on.
- **outline_note** if the project was redeployed after the run (task details then come from
  the current version).

Statuses: `created`, `running`, `canceled`, `failed`, `pending`, `ended_unexpectedly`,
`succeeded`, `stopping`, `completed` (`--status` filters, `--failed` = failed or
ended_unexpectedly).

## `ssisbliss outline`

```bash
ssisbliss outline STAGE/Load_Customer.dtsx
ssisbliss outline --env dev --folder BRONZE_API --project IMPORTAPI --package Fetch_Prices
```

Tasks in run order (from the precedence constraints, not the XML order, which is
alphabetical), with their kind (`sql`, `process`, `dataflow`, `script`, `sequence`,
`foreach`, `for`, ...), SQL statement and connection, process command (`runs`:
`powershell`, `python`, `cmd`, ...), data flow components, property expressions, what each
task runs after (`success`/`failure`/`completion`, with expressions), and the package's
connection managers with their expressions. Containers list their own tasks.

## Status

Unit-tested with msdb/SSISDB rows and packages taken from the lab, and run against SQL
Server 2019 (15.0.4470.1) in the lab: a chain broken by pointing STAGE at a read-only
history database, a Python API call that fails, and a job with a `/Par` override.
