# ssisbliss

**SSIS deploys that never overwrite your colleagues' work, and nightly loads explained when they
break. For people and AI agents.**

Tools for SQL Server SSIS catalogs and SQL Agent job chains, for the developers and DBAs who
deploy SSIS packages and keep the nightly loads running.

The tools are built to be safe to drive from code, including by LLM agents: read-only by
default, explicit plans, JSON output, and checks that refuse the dangerous cases.

## Tools

| Tool | What it does |
|---|---|
| [`ssisbliss`](docs/ssis-deploy.md) | Safe SSIS catalog deployment: deploy single packages, or a subset of a project, without overwriting anything else in the project or other people's work |
| [`ssisbliss build`](docs/build.md) | Build a Visual Studio SSIS project (.dtproj) into an .ispac without Visual Studio, matching Visual Studio's build on the server; deploy straight from a .dtproj |
| [`ssisbliss executions` / `outline`](docs/troubleshooting.md) | Why a package failed: the failing task, its error, its SQL or PowerShell/Python command, and the parameter values it ran with; what a package does, task by task |
| [`ssisbliss activity` / `strays`](docs/ssis-deploy.md#who-else-is-working-on-it-activity) | Who else is working on a package, or on a package that depends on it (their branches, whose copy the server has, who ran it lately); deployed packages nobody has any more, and removing them safely |
| [`ssisbliss lint` / `format` / `sql-diff`](docs/lint.md) | Lint packages and their SQL; lay the SQL out readably (only whitespace and keyword case change); the SQL changes as a readable diff for a pull request, where git shows each statement as one long line |
| [`ssisbliss jobs` / `chain` / `history`](docs/troubleshooting.md) | SQL Agent jobs and job chains: where the nightly chain is or where it broke, run history linked to SSIS executions |

Agents can use the tools through a shell (with the included skill, or `ssisbliss guide` and `ssisbliss
commands` without it) or as MCP tools (`ssisbliss mcp`); every command keeps one JSON output contract with
`ok`, `error_kind` and `next`: [docs/agents.md](docs/agents.md). How well AI agents (Claude Sonnet and Haiku,
Codex with GPT Luna) use them: [docs/agent-evaluation.md](docs/agent-evaluation.md).

## Lab

[`lab/`](lab/PLAN.md) creates an on-demand Windows + SQL Server environment on AWS that
matches field versions, with mock databases, SSIS catalog and Agent jobs to build and
test tools against. [docs/deploy-comparison.md](docs/deploy-comparison.md) compares Visual Studio's
and Microsoft's command-line deploy paths with `ssisbliss` on that lab.

`python lab/lab.py verify` runs every real-server check in one go (deploys, permissions,
builds, Agent chains, and Visual Studio's own Rebuild + Deploy Package driven through its UI)
and prints PASS/FAIL per check; it is run after tool changes and merged hill climbs.

## Install

Into any environment, with nothing added to your SSIS repository:

```bash
pip install "git+https://github.com/Samnor/ssisbliss@v0.1.0"
ssisbliss --help
ssisbliss install-skill            # optional: the ssisbliss skill for Claude Code and Codex (user level)
```

Or as a tool of its own: `uv tool install "git+https://github.com/Samnor/ssisbliss@v0.1.0"` (or
`pipx install` with the same URL). Leave out `@v0.1.0` for the latest from main. Needs git; each
[release](https://github.com/Samnor/ssisbliss/releases) also has the wheel to install directly.

Needs Python 3.11+. Talking to a server needs Microsoft ODBC Driver 17 or 18 for SQL Server;
`build`, `outline` on a file and `install-skill` work without it. SSISDB only accepts deploys
over Windows authentication, so deploys run on Windows (or Linux/macOS with Kerberos); reads
and plans also work with SQL logins. Tested against SQL Server 2019 (15.0.4470.1).

## Development

```bash
uv sync
uv run pytest
```

`lab/` and `climb/` (the AWS lab and the agent evaluation) are development tooling and are not
part of the installed package.

Real packages and names from the field stay in the gitignored `private/` folder, and
`python scripts/privacy_check.py --install-hooks` stops them from being committed or pushed:
see [docs/private-data.md](docs/private-data.md).

## License

MIT
