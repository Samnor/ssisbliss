# Changelog

## Unreleased

- `--folder` and `--project` may be left out, unlike Visual Studio's deploy wizard, which asks every
  time: the project is the `.dtproj`'s name, and the folder is where you last deployed or pulled that
  project on that server, else the environment's `folder` in `ssisbliss.toml`. The output says which
  ones were chosen and why (`target_from`). The server is still always named.

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
