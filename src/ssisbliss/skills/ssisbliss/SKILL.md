---
name: ssisbliss
description: Deploy SSIS packages safely to SQL Server catalogs (SSISDB) with ssisbliss, and find out why SQL Agent job chains or SSIS packages failed with ssisbliss chain, history and executions. Use for any request to deploy, build, roll back or remove SSIS packages or projects, put dev back to main, or explain a failed nightly load, a broken job chain or a failed package.
---

# ssisbliss

Every command prints one JSON document. Environments (dev, stage, prod) come from `ssisbliss.toml`:
pass `--env NAME` plus `--folder` and `--project` (catalog folder and project). Nothing changes
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
| clean up old / stray / leftover packages on dev | `ssisbliss strays --env dev --folder F --project P --dir path/to/dtsx`, then its `remove_command` (`remove-packages ... --strays`), plan then apply. It removes only verdict `stray`; tell the developer about the others and why they stay |
| prod | the same commands with `--env prod`; prod takes only committed and pushed packages and needs `--confirm-env prod` |

Package names: `Pkg` or `Pkg.dtsx` both work. A file named in a different case than the deployed
package is deployed as that package (with a warning).

## Why did it fail?

1. `ssisbliss chain --env prod` - where the job chain is: `completed`, `running: X`,
   `failed at X`, or `stopped after X` (with `why` when a step quits early). Running is not failing.
2. `ssisbliss history --env prod "JOB NAME" --runs 1` - the failed step and its
   `next` command (`ssisbliss executions --id N`). A failure on an earlier night: `--runs 5`,
   find the run on that date. A step with a `hint` and no execution never started (e.g. the
   package was renamed): run its `next` (`ssisbliss inspect ...`) and compare the names.
3. `ssisbliss executions --env prod --id N` - the failing task (its `package` may be a child
   package the job's package runs: report the child), its error, its SQL or the
   PowerShell/Python command, `connection_parameters` (with `override_note` when a job step
   overrides a parameter), and a `hint`. No messages at all usually means logging level None.
4. `ssisbliss outline --env prod --folder F --project P --package Pkg` - what the package does.

Report the cause in the developer's terms: which job, which task, what failed, and what to change
(a job step's `/Par` override, a read-only database, a script to run by hand on the server).
Do not deploy or change anything while diagnosing.
