# Changelog

## 0.1.0 (unreleased)

First release.

- `onprem-ssis`: plan-then-apply SSIS catalog deploys of single packages, project subsets,
  parameters and connection managers that never overwrite other people's work; `status`,
  `pull`, `remove-packages`, `rollback` (refused when it would undo others' deploys),
  `deploy-packages --from-ref`; per-user records that follow the repository across branches
  and worktrees; prod rules (committed, pushed, confirmed).
- `onprem-ssis build`: Visual Studio SSIS projects to `.ispac` without Visual Studio.
- `onprem-ssis executions` / `outline`, `onprem-agent jobs` / `chain` / `history`: why a
  nightly load or package failed, read-only.
- `onprem-ssis install-skill`: the ssisbliss skill for Claude Code and Codex.
