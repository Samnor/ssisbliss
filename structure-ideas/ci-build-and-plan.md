# CI build and plan

**Idea.** A pipeline on every PR that runs `onprem-ssis build` (no Visual Studio needed),
publishes the `.ispac`, and posts `onprem-ssis deploy-project --only <changed packages>` plans
against dev as a PR comment: what would change, what is protected, who else has the packages.

**Why it helps.** Reviewers see the deploy impact; merges to main can deploy to stage with the
same checks, and prod deploys come from a pushed, reviewed commit.

**Why not in the tools.** It needs a pipeline file and credentials in the repository's CI.
The tools stay usable from any shell; the commands above are what a pipeline would call.
