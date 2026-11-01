# How packages connect: `ssisbliss deps`

From one package, the packages it depends on and the packages that depend on it, followed link by link
through the whole project. Read-only; it reads your project folder, or the deployed project with `--env`.

```bash
ssisbliss deps STAGE/Stage_Customer.dtsx                     # your files: the package's folder
ssisbliss deps Stage_Customer --env dev                      # what is deployed (folder and project as for status)
ssisbliss deps STAGE/Stage_Customer.dtsx --format mermaid    # a diagram for a pull request or VS Code
ssisbliss deps STAGE/Stage_Customer.dtsx --format dot | dot -Tsvg > deps.svg
```

## What counts as a link

- **runs**: an Execute Package task in A runs B (A before B);
- **tables**: A writes a table that B reads, from Execute SQL tasks and data flow sources and destinations
  (A before B). Table names are compared as `schema.table` in lower case, `dbo` when no schema is given.

SQL built at run time (from a variable, an expression or a file), temp tables and table variables cannot be
followed. A package with such SQL is marked `unknown_sql` (dashed in the diagram) and the output's `note`
says that links through it are not shown. A child package an Execute Package task names but the project
does not have is marked `missing`.

## Options

| Option | |
|---|---|
| `--direction up` | only what the package depends on (what runs it, and what writes what it reads) |
| `--direction down` | only what depends on it: what may break when it changes |
| `--depth N` | follow links at most N steps out (default: all the way) |
| `--format mermaid` / `dot` | print the graph as text; the JSON also carries the Mermaid in `mermaid` |

## The JSON

- `nodes`: each package with its `role` (`upstream`, `this`, `downstream`, or `both`), `steps` from the
  package, and `unknown_sql` or `missing` when they apply; upstream first;
- `edges`: `from` runs or writes before `to`, with `kind`, the `tables` and `via` in words, e.g.
  "Stage_SalesOrder.dtsx reads stage.customer, which Stage_Customer.dtsx writes";
- `order`: a run order that respects every link, or `cycles` when packages feed each other.

It is quick: the links are indexed by table, so a project of 2000 packages takes well under a second
after its packages are read, and reading 200 packages takes about a tenth of a second.
