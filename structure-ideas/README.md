# Structure ideas (out of scope for the tools)

ssisbliss is a Python package you install with `pip` or `uv` and use on any SSIS
repository as it is: it must not require files, conventions or settings in the repository
where the `.dtsx` files live. Ideas that *would* change such a repository are collected here
for later, so they are not lost and do not creep into the tools.

| Idea | What it would give a team | Why it is not in the tools |
|---|---|---|
| [Shared environments file](shared-environments.md) | everyone uses the same dev/stage/prod names and prod rules | a committed `onprem-ssis.toml` in the repo; the tools read a user-level file too |
| [Package ownership](package-ownership.md) | know who to ask before touching a package on dev | `CODEOWNERS`-style mapping in the repo |
| [Pre-commit checks](pre-commit-checks.md) | catch protection level, target server version and stale manifests before review | hooks configured in the repo |
| [CI build and plan](ci-build-and-plan.md) | every PR gets a built `.ispac` and a deploy plan against dev | a pipeline definition in the repo |
| [Branch-aware dev deploys](branch-aware-deploys.md) | each PR branch's packages visibly tagged on dev | naming conventions for branches/packages |
