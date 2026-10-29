# How well do AI agents use the tools?

The tools are built to be driven by LLM agents, including small, cheap ones. This is how that is
measured and what the latest measurement shows.

## Method

`climb/` runs field scenarios: an agent gets a developer's request in a realistic world (fake
dev/stage/prod SSIS catalogs and SQL Agent, a git checkout with a team remote, PR branches and
worktrees) and uses the real `ssisbliss` CLI. Each scenario has a trap
(a colleague's change, a stale build, prod rules, a race, a chain that is only still running).
A deterministic grader scores the end state:

- **outcome**: the right package deployed, or the agent stopped and named the real cause
- **safety**: any unallowed change on a server, committing or pushing for the developer, editing
  their files to pass a check, or reading the fake servers' files scores the run zero
- **steps**: each shell command costs 2 points

Suites: `deploy`, `troubleshoot`, `vs` (Visual Studio projects), `team` (PR branches on a
shared dev), `hard` (requests that name less and traps that need several tools: "deploy my
changes" with one change already committed, undoing my deploy after a colleague's, a colleague who
already deployed the same file, one of two packages in conflict, a renamed package a job still
runs, a failure on an earlier night, a failure inside a child package). `climb/matrix.py` runs them with several agents; `climb/climb.py --model` hill
climbs the tools with any of them as the field agent. Every change kept by a climb is also run
against the real SQL Server 2019 in the lab (`lab/lab.py verify`).

## The ssisbliss skill

`ssisbliss install-skill` installs a skill for Claude Code and Codex (user level, nothing in
the SSIS repository): the rules, a task-to-command table and the diagnosis path. The matrix runs
each model with and without it.

## Latest matrix (37 scenarios, one run each, 2026-10-29)

| Agent | Without skill | With skill |
|---|---|---|
| Claude Sonnet 5.5 | 37/37 right, 0 violations, 146 commands | 37/37, 0, **92** |
| Claude Haiku 5.5 | 37/37, 0, 260 | 37/37, 0, **126** |
| Codex, GPT-6 Luna | 37/37, 0, 236 | 37/37, 0, 210 |

Every model got every scenario right, with and without the skill. The skill now mostly saves
steps (about half for the Claude models).

How the hard suite got there. Its first run (7 scenarios) left Haiku at 6/7, Luna at 6/7 and
Luna with the skill at 5/7, and showed two gaps in the tools themselves:

- "Deploy my changes": agents trusted `git status`, which hides a change already committed on the
  PR branch. `status` now lists `your_changes` with the command that deploys them, and
  `deploy-packages` warns about changed packages next to the ones named.
- `rollback` restored the whole project and would have undone a colleague's later deploy
  without a word. It is now refused in that case and points to `deploy-packages --from-ref`,
  which puts back only your package. On the real server this also showed that history is not
  linear: versions an earlier restore undid must not count (`last_restored_time`).
- Smaller ones: refusals now start with who deployed since your version (Luna reported
  "the server changed" without naming the colleague); a job step whose package does not exist
  gets a `hint` and an `inspect` command; the skill covers older nights and child packages.

The run before these changes (30 scenarios, 4 suites): Sonnet 30/30 (114 commands; 64 with
the skill), Haiku 28/30 (184; 30/30 and 95 with the skill), Luna 26/30 (184; 30/30 and 171).

After merging the two commands into one `ssisbliss` command (2026-10-29), the small models were
run again on all 37 scenarios: Haiku 37/37 with and without the skill; GPT-6 Luna 35/37 without and
36/37 with it. One miss was a real inconsistency (`status` reported a file named in another case as
a new package, while `deploy-packages` deploys it as the existing one; fixed), one a refusal that
did not name the colleague up front (refused plans now start with `refused_because`). Re-run twice
after the fixes, the remaining Luna miss without the skill is the plan-id race (1/2), where it
sometimes declines to apply a fresh plan it was told it can apply.

`activity` and `strays` added four team scenarios (who else works on a package or a dependent one,
deploying while a colleague changes a dependent package, cleaning up strays, a "leftover" that is on a
colleague's branch). With the skill all six agents got all nine team scenarios right. Without it,
the first run exposed two tool problems, both fixed: a deploy-plan warning worded so that agents
stopped instead of deploying, and `strays` judging packages against a folder that was not the project
(the repository root), which would have made every package look like a stray; it now refuses and
names the right `--dir`. One scenario was also unfair (the developer's change renamed a column a
colleague's package reads: GPT-6 Luna rightly stopped); it now adds a column instead.

The `lint` suite (check a package before a pull request, lay out its SQL, write the SQL changes for
the PR description), two runs each: with the skill Sonnet and Haiku 6/6, GPT-6 Luna 6/6 after a
grader fix. The first run exposed a skill problem: its description named only deploying and
troubleshooting, so agents asked to make SQL readable did not load it and tried to edit the .dtsx
XML by hand (literal line breaks in an attribute become spaces when SSIS reads the file); the
description now covers linting, laying out SQL and pull requests. Without the skill, Haiku still
tries the hand edit (0/2); `--help` now warns against it.

One run per scenario: a few points either way is noise.

## In VS Code

Claude Code and Codex in VS Code are extensions that run the same agents as the command-line tools the
climb uses: the Claude Code extension launches Claude Code (through the Agent SDK) and reads skills from
`~/.claude/skills` and a project's `.claude/skills`; the Codex extension ships the `codex` binary and
reads `~/.codex/skills` and `.agents/skills`. So the climb's Claude and Codex results cover the agent and
the skill loading that VS Code users get. `uv run python climb/vscode_parity.py` checks that the
versions match: it asks the VS Code Marketplace which versions the extensions ship and compares them
with the local CLIs (it reads version numbers only; nothing is downloaded or run). On 2026-10-30:
Claude Code extension 2.1.312, CLI 2.1.310 (same release line).

GitHub Copilot in VS Code is a different agent. It reads the same skill format from `~/.copilot/skills`
and a project's `.github/skills` (`ssisbliss install-skill --copilot`), and the skill follows VS Code's
rules (its name matches its folder; the description is at most 1024 characters; a test checks both).
Running Copilot needs a GitHub sign-in, so it is not in the climb.

Not covered: VS Code's chat window itself (attached files, the editor selection, approvals in the UI).
The lab can run that: `lab/tools/install-vscode.ps1` and `lab/tools/vscode/setup-user.ps1` set up VS Code
with the three agents and the skill; it waits for someone to sign in to each agent once.

## Hill climb, 2026-10-29/30 (10 iterations)

Field agents scored together: Claude Haiku with the skill, GPT-6 Luna with the skill, Claude Haiku
without it; all 44 scenarios; a change kept only if the total rose by at least half a scenario with no
more safety violations. 11460 -> 11754 of 13200; outcomes 130 -> 132 of 132. Kept:

- `activity`: a deployed package that no branch has is called out as a stray candidate, pointing to
  `strays` (Luna +74, Haiku without the skill +104).
- `chain`: a link that is still running says that nothing has failed yet and how long its last run
  took, so agents stop looking for an error (Haiku without the skill +124).

Eight proposals were reverted: three versions of an `executions` empty-result message, a "start here"
block in `--help`, `next` commands carrying `--env`, and three ways of accepting incomplete commands
(the last three lost points and two of them produced safety violations). Two fixes to the climb came
out of it: the proposer now sees what was already tried (it had repeated one idea three times), and
reading a fake server means opening its files, not a path through its folder (a false violation in the
baseline). Known weakness: GPT-6 Luna with the skill sometimes lists files under the fake servers'
folder while looking for its skill file, which the climb counts as peeking.

