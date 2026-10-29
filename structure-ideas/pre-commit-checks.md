# Pre-commit checks

**Idea.** A pre-commit hook (or `pre-commit` framework entry) running the checks `onprem-ssis`
already does at deploy time, on the files being committed:

- protection level is DontSaveSensitive (no user-key encryption committed)
- TargetServerVersion / component versions match the team's servers
- the `.dtproj` manifest is not stale for edited packages (what `onprem-ssis build` refreshes)
- package file names match the package names (the case mismatch the climb found)

**Why it helps.** Problems are caught when the change is made, not at deploy time on dev.

**Why not in the tools.** Hooks are configured in the repository. A read-only
`onprem-ssis check FILES` command would make this a one-line hook later.
