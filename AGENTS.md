# Working on ssisbliss

A Python CLI for SSIS catalogs and SQL Agent, used by people and LLM agents. Run tests with
`.venv/bin/python -m pytest -q`, about 30 seconds; the model checker and the climb's scenarios (slow, about
two minutes more) run with `-m "slow or not slow"`, as CI does. Run the tool with `.venv/bin/ssisbliss`. Add a CHANGELOG.md line under
Unreleased for anything a user would notice.

## Where things are

Everything is in `src/ssisbliss/ssis/` unless it says otherwise. Imports go down this list, never up, and
there are no import cycles.

| Layer | Modules |
|---|---|
| Entry | `cli.py`: every command's arguments, `run(argv)` (used by the CLI and MCP) and `main`. `mcp.py`: serves each parser entry as a tool, so a new command needs nothing there |
| Output | `contract.py`: `envelope()` shapes every JSON document (`ok`, `error_kind`, `next`) |
| Commands | `commands/`: `read.py` (read-only), `deploys.py` (plans and applies), `sqlcode.py` (lint, format, sql-diff), `about.py` (commands, guide), `common.py` (`reply`, `_env`, `_catalog`, `_target`, `Invocation`). SQL Agent commands: `../agent/cli.py` |
| A package's contents | `model.py`: `Package.parse(data, project)` is the one reader of a .dtsx: `connections` (with `database`), `walk()`, `every_task`, a task's `components`. `ProjectInfo` holds the values from outside a package. Use these, do not parse the XML again |
| What reads the model | `outline.py`, `lint.py`, `design.py` (design rules), `columns.py` (data flow columns), `deps.py`, `sqlsites.py` |
| Lint's vocabulary | `findings.py`: `RULES` and `Finding` |
| Servers | `catalog.py`: the `Catalog` protocol and `SqlCatalog`. `fake.py`: `FakeCatalog`, behind `fake://` servers and in tests. `deploy.py`: plans and applies against a `Catalog` |

## Recipes

- **A command**: add its parser and `set_defaults(fn=...)` in `cli.py`'s `parser()`, a `cmd_x(a)` returning
  `reply({...})` in `commands/`, and a row in `src/ssisbliss/skills/ssisbliss/SKILL.md`. `commands`, `--help`
  and the MCP tool follow from the parser. `a.invocation` has the typed `argv`, the parser and `target_from`.
- **A field on every document**: `contract.envelope()`.
- **A lint rule**: an entry in `findings.RULES`, then the check where it belongs: `design.py` for a package's
  design, `lint.py` for SQL text. `docs/lint.md` lists the rules.
- **A catalog query**: the method on the `Catalog` protocol and on `SqlCatalog` in `catalog.py`, and on
  `FakeCatalog` in `fake.py`. All three, or tests pass and real servers break.
- **A read of a deployed project**: `cat = _catalog(_env(a), a)`, `_target(a, env, cat, directory)` fills in
  folder and project, then `ProjectInfo.from_ispac(ispac, "", cat.parameter_bindings(id),
  cat.environment_values(id))`. For files: `ProjectInfo.from_folder(folder)`.

## Tests

Tests call `cli.main([...])` and read the JSON from stdout (`tests/test_contract.py` has the pattern), or build
a fake estate: `sql_world` (`climb/team.py`) and `medallion_world` (`climb/depscases.py`). Errors are JSON too:
raise `ValueError` for a refusal (exit 2).

Rules that hold everywhere: stdout is one JSON document; `ok` is true exactly when the exit code is 0;
nothing changes a server without a reviewed plan's `--plan-id`; read-only commands stay read-only.
