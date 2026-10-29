# Branch-aware dev deploys

**Idea.** Conventions so a shared dev server shows which PR branch each package came from:
branch naming (`pr/<ticket>-<name>`), and the tool stamping branch and commit into each
deployed package's `VersionComments` (visible in SSMS and Visual Studio).

**Why it helps.** "Whose Load_B is on dev, from which PR?" is answered by looking at the server;
after a merge, everything still tagged with that branch can be reset to `main`.

**Why not in the tools (yet).** Branch naming is a repository convention, and stamping changes
the deployed package. In scope instead: reading per-package history from the catalog's own
version store, which changes nothing, and `--from-ref` to put packages back to a git ref.
