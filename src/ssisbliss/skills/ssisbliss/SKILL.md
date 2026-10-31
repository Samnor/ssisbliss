---
name: ssisbliss
description: SSIS packages (.dtsx) and SQL Server SSIS catalogs (SSISDB) with the ssisbliss command. Use for any request to deploy, build, roll back or remove SSIS packages or projects; to check who else is working on a package or clean up stray packages on a server; to lint packages, lay out the SQL inside .dtsx files readably, or write the SQL changes of a pull request (never edit SQL in a .dtsx by hand); and to explain a failed nightly load, a broken SQL Agent job chain or a failed package.
---

# ssisbliss

Every command prints one JSON document. Environments (dev, stage, prod) come from `ssisbliss.toml`:
pass `--env NAME` plus `--folder` and `--project` (catalog folder and project). When the developer
did not say which folder or project, leave them out: ssisbliss takes the project from the `.dtproj`
and the folder from their last deploy, and says so in `target_from`; tell the developer which
target it chose. Nothing changes
on a server without `--apply` and the `plan_id` of a plan you just made.

## Rules (never break these)

1. **Plan, then apply.** Run the command without `--apply`; read `checks`; if `can_apply` is true,
   run the *same* command again with `--apply --plan-id <plan_id>` (plus `--confirm-env prod` on prod).
2. **A refused plan is an answer.** Exit 2 means a check failed: tell the developer what the check
   says. Do not work around it.
3. **Never overwrite a colleague's work.** If a check says someone else changed a package or
   parameter since your version, stop and name them. Use `--overwrite` only if the developer said so.
4. **Never commit, push, add a git remote, or edit the developer's files or project settings**
   (protection level, `.dtproj`) to get past a check. Those are the developer's decisions.
5. **Never read the catalog's or server's files directly**; use the commands.
6. **Plan id changed?** (exit 3) The server moved on: the output has a fresh plan whose checks
   ran again. If it can be applied, apply it with its new plan id; your package is the only one replaced.
7. **Someone else is on it?** A plan check `others_working:Pkg` (a warning) means a colleague's branch changes
   that package or one linked to it. It does not stop the deploy: apply the plan, then tell the developer who
   and which branch in your answer.

## Deploying

| The developer wants | Command (then the same with `--apply --plan-id ...`) |
|---|---|
| "deploy my changes" (no packages named) | `ssisbliss status --env dev --folder F --project P --dir path/to/dtsx`, then plan its `deploy_your_changes` command. `git status` misses changes already committed on the branch; deploy every package in `your_changes` |
| deploy one or more changed `.dtsx` (Visual Studio's Deploy Package) | `ssisbliss deploy-packages --env dev --folder F --project P path/Pkg.dtsx` |
| a new package | add `--allow-new` |
| deploy from the Visual Studio project, built first | `ssisbliss deploy-project --env dev --folder F --project P --dtproj path/X.dtproj --only Pkg` |
| deploy from a built `.ispac` (`bin/Development/X.ispac`) | `ssisbliss deploy-project ... --ispac path/X.ispac --only Pkg` |
| also a new or changed project parameter / connection manager | add `--item Name` (each one by name; the rest stay as deployed) |
| the whole project, like Visual Studio's Deploy | `deploy-project ... --dtproj X.dtproj --all` (still checks every package) |
| put dev back to main after a PR | `ssisbliss deploy-packages ... path/Pkg.dtsx --from-ref main` |
| remove a package their branch added | `ssisbliss remove-packages --env dev --folder F --project P Pkg` (someone else's leftover: `--strays Pkg`) |
| undo a bad deploy of one package | put back its earlier version from git: `deploy-packages ... path/Pkg.dtsx --from-ref main` (or the commit before yours) |
| undo a whole bad deploy | `ssisbliss versions ...`, then `ssisbliss rollback ... --to-lsn N`; refused if others deployed since (it would undo their work): then use the git way above |
| what is on dev compared to me | `ssisbliss status --env dev --folder F --project P --dir path/to/dtsx` |
| is anyone else working on Pkg, or on a package that depends on it? | `ssisbliss activity --env dev --folder F --project P --dir path/to/dtsx Pkg` (`--fetch` refreshes the team's branches first; no names: every package someone else touches). Report its `summary`: who, which branch, which linked package |
| clean up old / stray / leftover packages on dev | `ssisbliss strays --env dev --folder F --project P --dir path/to/dtsx`, then its `remove_command` (`remove-packages ... --strays`), plan then apply. It removes only verdict `stray`; tell the developer about the others and why they stay. `strays` already checked branches, callers, jobs and recent runs, so no `activity` check is needed first |
| prod | the same commands with `--env prod`; prod takes only committed and pushed packages and needs `--confirm-env prod` |

Package names: `Pkg` or `Pkg.dtsx` both work. A file named in a different case than the deployed
package is deployed as that package (with a warning).

## Reviewing packages and their SQL

| The developer wants | Command |
|---|---|
| check packages before a PR / review | `ssisbliss lint --changed path/to/dtsx` (all: without `--changed`). Report errors and warnings by package, task and line; do not fix them yourself unless asked |
| SQL in a package laid out readably | `ssisbliss format path/Pkg.dtsx` (only when they ask: it rewrites their file; `--check` changes nothing) |
| the SQL changes for a PR description | `ssisbliss sql-diff --formatted --markdown path/to/dtsx`: paste its output (git shows a package's SQL as one long line) |
| is this SQL valid on the server? will the data flows still match the tables? | `ssisbliss lint --env dev path/Pkg.dtsx` (SSB210: the server's own parser; SSB401-405: data flow columns against the server's tables) |

## Why did it fail?

1. `ssisbliss chain --env prod` - where the job chain is: `completed`, `running: X`,
   `failed at X`, or `stopped after X` (with `why` when a step quits early). A `why` is the answer:
   report it, with the job and step names it gives, and stop; `history` lists only the steps that ran.
   Running is not failing: with `running: X` there is no error or execution to look up yet.
2. `ssisbliss history --env prod "JOB NAME" --runs 1` (the chain's `next`) - the failed step and its
   `next` command (`ssisbliss executions --id N`). A failure on an earlier night: `--runs 5`,
   find the run on that date. A step with a `hint` and no execution never started (e.g. the
   package was renamed): run its `next` (`ssisbliss inspect ...`) and compare the names.
3. `ssisbliss executions --env prod --id N` - the failing task (its `package` may be a child
   package the job's package runs: report the child), its error, its SQL or the
   PowerShell/Python command, `connection_parameters` (with `override_note` when a job step
   overrides a parameter), and a `hint`. No messages at all usually means logging level None.
   `explained` gives the usual cause and fix for known errors (`real_error` is the error inside an
   OLE DB wrapper; `run_as` the account the run used): report it, but say it is the likely cause.
4. `ssisbliss outline --env prod --folder F --project P --package Pkg` - what the package does.

Before a failure, or after a deploy that renamed or removed packages or parameters:
`ssisbliss jobs --env prod --check --problems` lists job steps that will fail on their next run;
`ssisbliss health --env prod` checks the catalog (log cleanup, SSISDB size, logging level None,
executions stuck as running). Both are read-only: report their `fix` lines for the DBA, run none.

Report the cause in the developer's terms: which job, which task, what failed, and what to change
(a job step's `/Par` override, a read-only database, a script to run by hand on the server).
Do not deploy or change anything while diagnosing.
