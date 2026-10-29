# Shared environments file

**Idea.** Commit one `ssisbliss.toml` at the root of the SSIS repository with the team's
dev/stage/prod servers, expected `@@SERVERNAME`s and the prod rules (`require_git_pushed`,
`confirm`), so nobody types a server string and everyone gets the same guard rails.

**Why it helps.** One reviewed source of truth for which server is prod; new team members and
agents get the right setup by cloning.

**Why not in the tools.** It adds a file to the team's repository. The tools already find an
`ssisbliss.toml` in the working folder or its parents, and should also read a user-level
file (`~/.config/ssisbliss/ssisbliss.toml`), so they work without it.
