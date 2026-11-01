# Verifying the core promises

ssisbliss makes a few promises that matter more than any feature: it never overwrites a colleague's
deploy unseen, a plan you approved is the change that lands, and a refused command changes nothing. Tests
check examples of these; [`formal/model_check.py`](../formal/model_check.py) checks every sequence of
actions up to a depth, against the real code.

```bash
uv run python formal/model_check.py --depth 4
```

## What it does

Two developers who use ssisbliss (`me`, `ann`), each with their own working folder and base records, and
a colleague who deploys from Visual Studio (`bob`), share one SSIS project with two packages on a fake
catalog. The actions:

| Action | What runs |
|---|---|
| `edit(who, package)` | the developer changes their copy (up to twice per package) |
| `pull(who)` | `ssisbliss pull` |
| `deploy(who, package)` | `ssisbliss deploy-packages`, then the same with `--apply --plan-id` |
| `plan(who, package)` | the plan only; its id is kept |
| `apply_later(who)` | the kept plan id applied now, whatever happened in between |
| `rollback(who, lsn)` | `ssisbliss rollback --to-lsn`, plan then apply, to every stored version |
| `bob_deploys(package)` | a deploy outside ssisbliss |

Every sequence up to `--depth` is run (states already explored at that depth are skipped), each action
through `cli.run`: the code the command line and the MCP server run. After every action the properties
are checked against a ghost model the checker keeps on its own: who wrote each version on the server, and
every version each developer's work started from.

## The properties

Safety:

- **no-lost-work**: a command by X replaces a package's server copy only if X wrote that copy, or X's work
  started from it. Nobody's deploy is overwritten or rolled back without them having built on it.
- **scope**: `deploy-packages` changes no package but the one deployed.
- **plan-binding**: an apply with a plan id changes nothing if the server moved since the plan.
- **refusal-atomic**: a command that does not succeed leaves the server as it was.
- **contract**: `ok` is true exactly when the exit code is 0, `error_kind` is there when it is not, and an
  applicable plan's `next` carries its plan id.

Progress (the features work, not only refuse):

- **deploy**: X's deploy of a changed package goes through when the server copy is the one X's work
  started from, whatever else changed in the project.
- **apply-later**: a plan applied later goes through when neither the server nor the file changed.
- **rollback-own**: X can roll back to a version when every version after it is X's own.
- **pull** always works.

A violation prints the property and the shortest sequence that breaks it, for example
`edit('me', 'Load_A.dtsx') -> bob_deploys('Load_A.dtsx') -> deploy('me', 'Load_A.dtsx')`.

## Does it catch bugs?

`tests/test_formal.py` checks depth 3 on every test run, then takes a safety check out of the tool and
requires the checker to find the break:

| Bug planted | Found as | Depth |
|---|---|---|
| deploy-packages ignores your base (as with `--overwrite` on every package) | no-lost-work | 3 |
| rollback undoes others' deploys (as with `--undo-others`) | no-lost-work | 2 |
| the plan id leaves out the server's version | plan-binding | 4 |

Building the checker also showed where it was too weak: its first version changed packages only in XML
comments, which the package hash rightly ignores, so no edit was ever a change and the planted bugs went
unseen. The planted-bug tests are what caught that.

## What it found

At depth 5, a real bug: deploy your change, roll your own deploy back (it broke the nightly load), fix it
and deploy again, and the tool refused, saying the server copy had changed because "someone else deployed
it". Your base record still pointed at your rolled-back version. Now a project version you restored
yourself (`restored_by` in `catalog.object_versions` is your login), from before your version, no longer
blocks your next deploy; a rollback by someone else still does. The lab checks that SSISDB records the
restore under the login the tool sees.

## Runs

| Depth | Actions run | Distinct states | Violations | Time (laptop) |
|---|---|---|---|---|
| 3 | 706 | 41 | 0 | 46 s |
| 4 | 3162 | 184 | 0 | 3 min |
| 5 | 12814 | 733 | 0 | 12 min |

CI runs depth 4 on every push (`model-check` in `.github/workflows/ci.yml`).

## What it does not cover

The fake catalog stands in for SSISDB; `lab/lab.py verify` checks the same flows against SQL Server 2019,
including the MCP server. Not modelled: project parameters and connection managers (`--item`), builds
from a `.dtproj`, running executions, git rules for prod, and more than two packages. Depth bounds the
sequences: a break that needs more steps than `--depth` is not found.
