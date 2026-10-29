# Package ownership

**Idea.** A `CODEOWNERS`-style file mapping packages (or folders of packages) to people or
teams, e.g. `STAGE/Stage_Ledger*.dtsx  @finance-data`.

**Why it helps.** When a plan is refused because someone else changed a package on dev, the
tool could say who owns it, not only who deployed last; PR review could require the owner.

**Why not in the tools.** It is a convention in the repository. The tools can still report
per-package history from the catalog (who changed which package, when) without it.
