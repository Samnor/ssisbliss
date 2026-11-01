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
| `jobs --check [--problems]` | each job step checked against the catalog it runs from: a package, folder or project that is not deployed (renamed or removed by a deploy, with the closest deployed names), a `/Par` override naming a parameter or connection manager the package or project no longer has, an `/ENVREFERENCE` the project does not have, and a step that jumps to a step that does not exist. Each is a `problem` on the job with the step and the fix; all of them fail the next run. A step on another server is a `note` (not checked). `--problems`: only jobs with problems |
| `chain [--name X]` | job chains linked by `sp_start_job` steps, in order, each link's state in the current or last run (`succeeded`, `failed`, `running`, `not_started`) and a verdict: `completed`, `running: X`, `failed at X`, `stopped after X: Y did not start`. When a chain stopped because X's start-next step is skipped on success, `why` says which step quits the job. When a link is still running, `why` says nothing has failed (no error exists yet; `history` lists only finished runs) and gives its last run's duration (`last_run_took_s`) so an overrun shows. Steps that start a job that does not exist are listed as `missing_jobs` |
| `history JOB [--runs N] [--failed]` | recent runs with every step's outcome and message; SSIS steps get their `ssis_execution_id`, failed ones the `ssisbliss executions --id` command; a step whose package was not found (renamed or removed by a deploy, so no execution exists) gets a `hint` and an `ssisbliss inspect` command; `skipped_starts` names a step that starts another job but never runs when the job succeeds, since runs list only the steps that ran; a step that took at least 1.5 times (and a minute more than) its median in the other runs listed is marked `slower`, with the `ssisbliss slow --id` command for an SSIS step |

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
- **database**: the database the failing task's connection pointed at in that run (for a failing data
  flow component, its own connection's, named in `database_connection`): the connection's expression or
  connection string evaluated with the run's parameter values, overrides included, as `outline` and `deps`
  work it out.
- **override_note**: when one of those parameters was overridden, says that the value came
  from the caller (e.g. the SQL Agent job step's `/Par "$Project::TargetDb";...`), not from
  the project or environment, and points at `ssisbliss jobs` to see it. The fix belongs
  in the job step (or the database it points to); redeploying the package won't change it.
- **hint**: for PowerShell/Python/other process tasks, SSIS logs only the exit code, not
  the process's own error output (unless the task has a `StandardErrorVariable`). The hint
  gives the exact command line and machine to rerun it on.
- **explained**: for errors SSIS logs without saying why, the usual cause and the fix
  (`package_errors_explained` for package-level errors). `0xC0202009`/DTS_E_OLEDBERROR only
  wraps the real error, so its `real_error` is the OLE DB record's Description. Others:
  VS_NEEDSNEWMETADATA (a table changed after the build), truncation and conversion errors,
  "provider is not registered" (the 64-bit Excel/ACE driver missing on the server, or the
  job step's 32-bit setting), login, database and object permissions, files and mapped drives,
  no environment selected in the job step, script tasks built for another SQL Server version,
  user-key encryption, deadlocks, timeouts, duplicate keys, NULLs, foreign keys, memory. Many
  "works in Visual Studio, fails in the job" errors come from the run's account: errors about
  logins, permissions and files carry `run_as`, the account the run used.
- **outline_note** if the project was redeployed after the run (task details then come from
  the current version).

Statuses: `created`, `running`, `canceled`, `failed`, `pending`, `ended_unexpectedly`,
`succeeded`, `stopping`, `completed` (`--status` filters, `--failed` = failed or
ended_unexpectedly).

## Slow, not failed: `ssisbliss slow`

`slow --env ENV --id N` (an execution id from `history`) or `slow --env ENV --folder F --project P
--package Pkg` (its newest finished run) compares each task's time in that run with its median over
the package's earlier successful runs (`--runs`, default 20), and names the tasks that took the extra
time: `slower_tasks` with `took`, `usual` and `extra_s`; `times` when a loop ran more often than
usual; `rows` and `why: more rows than usual` when the runs logged row counts. `verdict` says it in one
line, e.g. `2h31m (usually 31m43s): Load Customer took 2h10m, usually 10m00s (more rows than usual)`.
Containers are not blamed (their time is their tasks'); `new_tasks` were not in enough earlier runs.
`deployed_since_last_run` lists the project versions deployed between the last usual run and this
one (who and when); `no: ...` means the packages are the same, so the data or the server changed.

Task times are kept from logging level Basic up (`catalog.executable_statistics`); row counts only at
Performance or Verbose. A run at logging level None has only its total, and `slow` says so.

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

Each connection also has the `database` it points at when that can be worked out (as `deps` does): for a
file, from its expression over the project parameters in `Project.params` next to it (then `database_from`
says it is a value in your files, which the catalog may replace); for a deployed package, from the
catalog's values and the SSIS environments they reference.

## Status

Unit-tested with msdb/SSISDB rows and packages taken from the lab, and run against SQL
Server 2019 (15.0.4470.1) in the lab: a chain broken by pointing STAGE at a read-only
history database, a Python API call that fails, and a job with a `/Par` override.

## Catalog health: `ssisbliss health`

`health --env ENV` reads the catalog's settings and state and says what will break loads before it does,
each warning with the T-SQL for the server's DBA (it changes nothing):

| Check | Warns when |
|---|---|
| `logging_level` | the default logging level is None (a failed run records no error) or Verbose (fills SSISDB) |
| `size`, `log` | SSISDB's data is 10 GB or more; its log is large and waits for a log backup under FULL recovery |
| `cleanup`, `cleanup_job`, `cleanup_lag` | operation cleanup is off; the `SSIS Server Maintenance Job` is missing, disabled, unscheduled or failed last time; the oldest operation is older than the retention |
| `stuck_execution` | an execution has been running or pending for `--stuck-hours` (24): usually a restart cut it off; the fix is `catalog.stop_operation` |
| `startup_procedure` (info) | `sp_ssis_startup` is not a startup procedure, so a restart leaves executions "running" |

`verdict` is `healthy` or the number of warnings; `catalog` has the numbers (retention, cleanup, logging
level, sizes, recovery model, operation count and age, open executions, the cleanup job). Parts the login
may not read are listed in `not_checked`.
