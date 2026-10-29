# Safe SSIS deployment (`ssisbliss`)

Two deployment tools for the SSIS catalog (SSISDB), built so a person **or an LLM agent**
can deploy with code only, with guard rails that make the dangerous mistakes impossible
or at least loud.

| Tool | Input | Server call | Everything else in the project |
|---|---|---|---|
| `deploy-packages` | one or more `.dtsx` files | `catalog.deploy_packages` | kept byte for byte by the server |
| `deploy-project --only ...` / `--all` | a built `.ispac` or a `.dtproj` ([built here](build.md)) + the packages to take from it, or all of them | `catalog.deploy_packages` (default), or `catalog.deploy_project` with a merged `.ispac` when project parameters / connection managers must change | kept: every package not in `--only` stays as currently deployed |

The point of the second tool: a full project deploy from a developer's `.ispac` replaces
**every** package in the project with whatever was in that build. In real estates the
deployed project often contains packages that were hot-fixed on the server, or that the
developer's branch doesn't have. `deploy-project --only` takes just the packages you name
and leaves the rest alone. Names are case-sensitive; the `.dtsx` extension is optional
(`--only Stage_Product` means `Stage_Product.dtsx`).

`deploy-packages` treats a file whose name differs from a deployed package only in case
(`stage_customer.dtsx` vs `Stage_Customer.dtsx`) as that package, since the catalog ignores
case in package names: the plan updates `Stage_Customer.dtsx`, finds its base record, and
adds a `name:` warning suggesting a rename. It never adds the file as a second package.

## Plan, then apply

Every command is read-only unless `--apply` is given, and `--apply` requires the `plan_id`
from a plan run:

```bash
ssisbliss deploy-packages --server "host,14331" --folder STAGE --project STAGE Load_Customer.dtsx
# -> JSON plan with checks, changes, retained packages and "plan_id": "3f9c..."
ssisbliss deploy-packages --server "host,14331" --folder STAGE --project STAGE Load_Customer.dtsx \
    --apply --plan-id 3f9c...
```

The plan id is a hash of the target server name, folder, project, its current
`object_version_lsn`, the deployment mode and the content hash of every package being
deployed. If anything changed between plan and apply (someone else deployed, a file was
edited, the wrong server was given) the ids differ and apply refuses. An agent cannot skip
straight to apply, and a human reviewing a plan knows exactly what will run.

## Working in a team: dev, stage, prod

The typical estate: several developers deploy work-in-progress packages to dev and stage
again and again, and git only gets the package when it is ready for prod. The server, not
git, holds the latest truth, so the biggest risk is overwriting a colleague's deployed work.

**Overwrite protection.** `ssisbliss` remembers, per user, which deployed version of each
package (and project parameter / connection manager) you started from: written by `pull` and
after each of your own deploys, kept in your state folder (`$SSISBLISS_STATE_DIR`, default
`~/.ssisbliss/state`), never in the repository. Records are per repository (its `origin`
URL, else its git directory), so every worktree, PR branch and clone of the same repository
knows your own deploys. Updating a package whose server copy no longer matches your version
is an **error**, listing who deployed since:

```text
overwrite:Load_Customer.dtsx  error  the server copy changed since you pulled or deployed it
  (someone else deployed it); deployed since your version (lsn 812): lsn 815 by DOMAIN\colleague
  at 2026-10-27T14:02. Pull and merge, or pass --overwrite Load_Customer.dtsx
```

With no record at all (first time on this machine) it is also an error until you `pull` or
name the package with `--overwrite`. Iterating on your own deploys never trips it.
(`.ssis-base.json` files next to the packages, from earlier versions, are still read.)

```bash
ssisbliss pull --env dev --folder STAGE --project STAGE --out ./STAGE --packages Load_Customer.dtsx
# local file missing or identical -> written/recorded
# local file differs -> server copy saved as Load_Customer.server.dtsx, your file untouched
ssisbliss pull ... --mark-merged Load_Customer.dtsx   # after merging the server copy into yours
```

**Environments.** `ssisbliss.toml` names the servers so nobody types a prod server string by
accident. It is read from the working folder or a parent, else from your own
`~/.config/ssisbliss/ssisbliss.toml` (or `$SSISBLISS_CONFIG`), so nothing has to be
added to the SSIS repository:

```toml
[environments.dev]
server = "devsql01\\SQL01"
server_name = "DEVSQL01\\SQL01"      # expected @@SERVERNAME; a wrong alias is refused

[environments.stage]
server = "stagesql01\\SQL01"
server_name = "STAGESQL01\\SQL01"

[environments.prod]
server = "prodsql01\\SQL01"
server_name = "PRODSQL01\\SQL01"
require_git_committed = true          # only packages committed and unmodified in git
require_git_pushed = true             # ... whose last commit is on a remote branch (pushed)
confirm = true                        # apply also needs --confirm-env prod
```

## Working on PR branches

Many developers, each on a PR branch, deploying iteratively to the same dev or stage server:

| Situation | Command |
|---|---|
| deploy the packages your branch changed | `deploy-packages --env dev ... STAGE/Load_A.dtsx STAGE/Load_B.dtsx` (or `deploy-project --dtproj X.dtproj --only Load_A,Load_B`) |
| your branch adds a project parameter or connection manager | `deploy-project ... --dtproj X.dtproj --only Load_A --item BatchSize` (only that item; a colleague's new parameter stays) |
| see what is on dev compared with your version and your files | `status --env dev --folder F --project P --dir STAGE`: lists `your_changes` (committed on the branch or not; `git status` shows only the uncommitted ones) and the `deploy_your_changes` command. `deploy-packages` also warns about changed packages next to the ones you named |
| your deploy broke something, and others deployed since | `deploy-packages ... STAGE/Load_A.dtsx --from-ref main` (or the commit before yours) puts back only your package; `rollback` restores the whole project and is refused when it would undo someone else's later deploy (`--undo-others` if they agreed) |
| a colleague changed the same package | the plan stops and names who deployed since your version; agree with them, then `pull` and merge, or `--overwrite` |
| your PR is merged: put dev back to main | `deploy-packages ... STAGE/Load_A.dtsx --from-ref main` |
| your PR is abandoned: remove the package it added | `remove-packages --env dev --folder F --project P Load_New` (only if the server copy is yours) |
| is anyone else working on a package, or on one that depends on it? | `activity --env dev --folder F --project P --dir STAGE Load_A` (see below) |
| clean up packages nobody has any more | `strays --env dev --folder F --project P --dir STAGE`, then `remove-packages ... --strays` (see below) |
| switch branch or worktree and keep going | nothing to do: your records follow the repository |

Other packages and project items on the server are never touched by any of these.

## Who else is working on it? (`activity`)

```bash
ssisbliss activity --env dev --folder STAGE --project STAGE --dir STAGE Stage_Customer [--fetch]
ssisbliss activity --env dev --folder STAGE --project STAGE --dir STAGE      # every package someone else touches
```

For each package, read-only:

- **branches**: the team's remote branches that change it compared with the default branch, with the
  author of the last change, when, and how many commits (`--fetch` runs `git fetch` first; otherwise
  the branches are as of your last fetch);
- **server**: whose version the server has, matched by content: the default branch's, yours (your base
  record), a version from someone's branch (named), or a version on no branch at all (someone's
  unpushed work, with the latest project deploys and who made them). A package that no branch has at
  all, not even in another version, is a stray candidate: the state says so and points to `strays`,
  which decides from callers, jobs and recent runs. SSISDB records who deployed the
  project and when, but not which packages a deploy contained, so content is how the tool tells;
- **recent_runs_by_people**: catalog executions of the package started by people in the last
  `--days` (14), leaving out SQL Agent's own runs (its service account and proxy accounts);
- **jobs**: SQL Agent job steps that run it;
- **depends_on / used_by**: packages linked to it by an Execute Package task, or by a table one writes
  and the other reads (from Execute SQL tasks and data-flow sources, destinations and lookups), each
  with anyone working on it; including links that only exist on a colleague's branch. SQL built at run
  time (expressions, variables, files) cannot be followed and is listed under `not_followed`;
- **summary**: the above in one sentence.

Deploy plans run the same check for every package they deploy: a colleague on it or on a linked
package gives a warning `others_working:<package>` (never an error: parallel work is normal, and
overwriting someone's deployed work is already refused).

## Stray packages (`strays`, `remove-packages --strays`)

A stray is a deployed package that is not in the default branch and not in your working copy, is on
no branch, is called by no other package, is run by no SQL Agent job, and that nobody ran in the
last `--days` (30). `strays` lists every package that is not in the default branch with that evidence
and a verdict (`stray`, `check first`, `keep for now`), plus the commit that removed it from the
default branch when there is one, and a `remove_command` for the strays:

```bash
ssisbliss strays --env dev --folder STAGE --project STAGE --dir STAGE
ssisbliss remove-packages --env dev --folder STAGE --project STAGE --dir STAGE --strays [Pkg ...]
```

`remove-packages --strays` (no names: every stray) re-checks each package in the plan and refuses one
that is not a stray, naming why (whose branch, which caller or job, who ran it when);
`--overwrite Pkg` removes it anyway once its owner agreed. Like any removal it is a project redeploy
of the deployed project minus those packages, with a backup and verification.

## Checks (all run in the plan)

- **Overwrite protection** as above (`--overwrite PACKAGE` to replace knowingly).
- **Environment**: connected server matches `server_name`; prod-like environments only take
  packages committed in git, and with `require_git_pushed` only when that commit is on a
  remote branch, so "ready for prod" means what the team already means by it. If a file is
  uncommitted or unpushed the plan stops and the developer has to commit or push it. An agent
  must not commit or push on the developer's behalf to get past this check; in the lab's model
  comparison a small model (Claude Haiku) committed for the developer to pass a commit-only rule,
  which a pushed-commit rule turns into a refusal unless it also publishes the developer's work.
  The check uses the local remote-tracking refs: after pushing from another clone, `git fetch`.
- **Target**: SSISDB exists; folder and project exist; caller is `ssis_admin`, `sysadmin`, or
  has MODIFY on the project (what a folder grant of `MODIFY_OBJECTS` gives a developer).
- **No running executions** of the project (override `--allow-running`).
- **Package names**: each file must match a package already in the project, unless
  `--allow-new`. Catches typos that would silently add a duplicate package.
- **XML and format**: well-formed `.dtsx`, `PackageFormatVersion` supported by the server,
  no component assembly versions newer than the server (a package built for 2022
  targeting a 2019 server fails at run time, not at deploy time).
- **Protection level**: refuses `EncryptSensitiveWithUserKey` (sensitive values are lost or
  unreadable on the server) and the password-based levels. `DontSaveSensitive` is expected;
  `ServerStorage` (what the catalog exports) is accepted because such a file holds no
  sensitive values. `pull` writes packages back as `DontSaveSensitive` so they fit a Visual
  Studio project.
- **References**: every `$Project::Param` and `Project.ConnectionManagers[...]` the package
  uses must exist in the deployed project.
- **Unchanged packages** are reported as `unchanged` and skipped.
- `deploy-project`: project parameters / connection managers that differ between the
  `.ispac` and the server are reported. They are only deployed with
  `--project-items update`, which switches to a merged full-project deploy and warns if
  the server copy holds sensitive values that an export cannot carry over.

## Apply

1. Recompute the plan; refuse on errors or `plan_id` mismatch.
2. **Backup**: download the deployed project (`catalog.get_project`) and save it with the
   plan and a snapshot of parameter bindings and environment references, under
   `--backup-dir` (default `~/.ssisbliss/backups`).
3. Deploy.
4. **Verify**: operation succeeded; new version LSN; every package that existed before
   still exists; deployed packages match the new content; untouched packages match their
   previous content; parameter bindings and environment references are unchanged.
5. Output includes the exact `rollback` command (`catalog.restore_project` to the previous
   LSN). A rollback replaces every package, so its plan is refused when someone else deployed
   after the version you restore (versions an earlier restore already undid do not count).

## Exit codes

`0` ok · `1` unexpected error · `2` refused by a check · `3` plan id mismatch ·
`4` deployed but verification found differences (see output, consider rollback).

## Connecting

`--env NAME` or `--server` (passed to ODBC as is: `host,port` or `host\instance`). Windows
auth by default; `--sql-user NAME` uses SQL auth with the password from
`SSISBLISS_SQL_PASSWORD` (never on the command line). `--trust-server-certificate` for lab
servers with self-signed certificates.

SSISDB refuses `deploy_project`, `deploy_packages` and `restore_project` for SQL logins
(error 27123), so `--apply` needs Windows authentication. Plans, `inspect`, `versions` and
`pull` work with either.

## How packages are compared

SSISDB re-saves every package of a project whenever any of them is deployed: attributes
reordered, `ProtectionLevel` set to `ServerStorage`, a new `VersionGUID`,
`LastModifiedProductVersion` set to the server's version, connection properties its runtime
does not know dropped, and expression-driven values recalculated from the deployed parameters
([build.md](build.md#rebuild-then-deploy-package)). The bytes you deploy never come back, and
packages you did not deploy change too. Every comparison (unchanged detection, overwrite
protection, base records, verification) uses a content hash of the canonical XML (C14N)
without any of those.

## Status

Unit-tested against a fake catalog (`tests/`) that mimics what the real one does, and run
end to end against SQL Server 2019 (15.0.4470.1) in the lab: `lab/lab.py install-tools`,
then `lab/lab.py run lab/tools/smoke-ssis.ps1`. That run covers pull, a package deploy,
a subset project deploy (both `keep` and the merged `update`), a refused plan with no base
record, and rollback, each with verification. `lab/tools/devlogin-ssis.ps1` repeats a
deploy as a Windows user with only folder permissions.

Behaviour of the real catalog that the lab found, and the tool now handles:

- `catalog.deploy_packages` appends `.dtsx` to each name itself; passing `X.dtsx` creates a
  second package `X.dtsx.dtsx`.
- The stored package differs byte for byte from the deployed one (see above).
- `catalog.deploy_project` rejects a `@Project.manifest` that starts with any XML
  declaration.
- A failed deployment only points at `catalog.operation_messages`; the tool reads those and
  puts them in the error.
