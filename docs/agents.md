# ssisbliss for agents

ssisbliss treats LLM agents as first-class users. The recommended way is the skill: install it once and let
the agent run `ssisbliss` in its shell.

```bash
ssisbliss install-skill
```

The other ways run the same commands, for when the skill is not an option:

| Way | When | How |
|---|---|---|
| **The skill (recommended)** | the agent has a shell and loads skills (Claude Code, Codex, GitHub Copilot) | `ssisbliss install-skill` |
| `guide` and `commands` | the agent has a shell but does not load skills | `ssisbliss guide`, `ssisbliss commands` |
| MCP | the client cannot run commands, or should not have a shell | `ssisbliss mcp` as an MCP server |
| Python | your own code | `from ssisbliss.ssis.cli import run` |

Why the skill: it puts the rules and the task-to-command table in front of the agent before it starts, where
over MCP the agent has to call `guide` itself, and not every agent does. In the climb's 54 scenarios
([agent-evaluation.md](agent-evaluation.md), 2026-11-01), average score per run out of 100:

| Agent | Skill and CLI | MCP |
|---|---|---|
| Claude Haiku 5.5 | **92.7** | 90.0 |
| Codex, GPT-6 Luna | **89.0** | 85.1 |

Most of Luna's gap over MCP is package review: it read and edited the `.dtsx` XML itself instead of calling
`lint` or `format`. Codex also asks before every call to an MCP tool marked destructive, plans included (see
below). Both ways had no safety violations: the plan id and the tools' refusals guard the server either way.
Having both the skill and the MCP server is not measured.

## The output contract

Every command keeps the same contract, whichever way it is called (`ssisbliss commands` prints it, with
`schema_version`):

- stdout is one JSON document (only `install-skill --print`, `sql-diff --markdown` and `deps --format mermaid|dot` print text);
- `ok` is true exactly when the exit code is 0;
- when it is false, `error_kind` says why, and `error`, a plan's `refused_because` or lint's `findings` say what;
- `next`, when there is one, lists what to do next: commands ready to run, already carrying this command's
  `--env` or `--server`, or an action for the developer. A plan that can be applied has the apply command
  there, with its `plan_id`, and `apply_when` says when to run it: when the developer asked for the change,
  not only for a plan or a check.

| Exit | `error_kind` | Meaning |
|---|---|---|
| 0 | (`ok`: true) | done; for a deploy without `--apply`, a plan that can be applied |
| 1 | `error` | unexpected, or the server could not be reached |
| 2 | `refused` | not done: a check, the input or the target said no, or lint found problems |
| 3 | `plan_mismatch` | `--plan-id` is not the plan the command makes now: plan again |
| 4 | `verify_failed` | applied, but the server does not hold what was deployed |
| 5 | `usage` | unknown command or option, or a missing argument; `next` has the help to read |

A mistyped command is JSON too, with the closest commands in `next`:

```json
{"ok": false, "error_kind": "usage",
 "error": "no command 'deploy'; did you mean: deploy-project, deploy-packages, slow",
 "next": ["ssisbliss deploy-project --help", "ssisbliss deploy-packages --help", "ssisbliss slow --help",
          "ssisbliss commands"]}
```

## Without the skill: `guide` and `commands`

`ssisbliss guide` prints the skill's rules and recipes as JSON (`guide rules`, `guide deploy`,
`guide review`, `guide troubleshoot` for one part). `ssisbliss commands` lists every command, whether it
changes a server or writes files, and the exit codes; `ssisbliss commands deploy-project` gives one
command's arguments with their types, choices and defaults. Running `ssisbliss` with no command points at
both.

## MCP (when the agent cannot use a shell)

`ssisbliss mcp` serves every command as an MCP tool over stdio, with no extra package to install. A tool
is named after its command with underscores (`deploy-packages` is `deploy_packages`); its arguments are the
command's options without the dashes (`--plan-id` is `plan_id`), plus `cwd`, the folder to run in. The
result is the command's JSON document (also as structured content); `isError` is true when `ok` is false.
Read-only commands are marked read-only; deploys are marked destructive, and like the CLI they change
nothing without `apply` and the `plan_id` of the plan they just returned. The server's instructions tell
the agent this and point at `guide`.

Claude Code:

```bash
claude mcp add ssisbliss -- ssisbliss mcp
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.ssisbliss]
command = "ssisbliss"
args = ["mcp"]
```

Codex asks before each call to a tool marked destructive, which includes the deploy tools' plans. In
`codex exec`, where nobody can answer, those calls fail ("requires approval, but approval policy is
never") unless the server's calls are approved up front with `default_tools_approval_mode = "approve"`
under `[mcp_servers.ssisbliss]`: the plan id and the tool's refusals still stand between the agent and
the server.

VS Code (`.vscode/mcp.json`):

```json
{"servers": {"ssisbliss": {"type": "stdio", "command": "ssisbliss", "args": ["mcp"]}}}
```

The server runs commands in the folder the client starts it in (usually the repository), with the
environment it is given: `SSISBLISS_SQL_PASSWORD` for SQL logins, and `ssisbliss.toml` found as on the
command line.

## Python

`ssisbliss.ssis.cli.run(argv)` runs one command and returns its answer (`doc`, `code`, or `text`) without
printing; the CLI and the MCP server are both thin layers over it. The commands live in
`ssisbliss/ssis/commands/` (read, deploys, sqlcode, about, skill), one function per command.

## How it is measured

The climb ([agent-evaluation.md](agent-evaluation.md)) runs small models through every scenario with the
skill, without it, and over MCP, and keeps a change only when they do better together.
