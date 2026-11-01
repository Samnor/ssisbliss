# How packages connect: `ssisbliss deps`

From one package, the packages it depends on and the packages that depend on it, followed link by link
through the whole project. Read-only; it reads your project folder, or the deployed project with `--env`.

```bash
ssisbliss deps STAGE/Stage_Customer.dtsx                     # your files: the package's folder
ssisbliss deps Stage_Customer --env dev                      # what is deployed (folder and project as for status)
ssisbliss deps STAGE/Stage_Customer.dtsx --format mermaid    # a diagram for a pull request or VS Code
ssisbliss deps STAGE/Stage_Customer.dtsx --format dot | dot -Tsvg > deps.svg
```

Several projects, such as the layers of a medallion architecture in their own databases:

```bash
ssisbliss deps silver/SILVER/Load_Customer.dtsx --dir bronze/BRONZE --dir gold/GOLD          # your files
ssisbliss deps Load_Customer --env prod --folder SILVER --project SILVER --also BRONZE/BRONZE --also GOLD/GOLD
ssisbliss deps Load_Customer --env prod --folder SILVER --project SILVER --all-projects        # the whole catalog
```

With several projects, packages are named `PROJECT/Package.dtsx` (`FOLDER/PROJECT/...` when two projects
share a name). A package name found in more than one project needs its path, or `--project`.

## What counts as a link

- **runs**: an Execute Package task in A runs B (A before B);
- **tables**: A writes a table that B reads, from Execute SQL tasks and data flow sources and destinations
  (A before B), in the same or another project. Execute Package calls stay within a project.

## Which database a table is in

Tables are compared as `database.schema.table` in lower case (`dbo` when no schema is given), so every
layer's `dbo.Customer` is a different table. The database is:

1. the one the SQL names (`SELECT ... FROM BRONZE.dbo.Customer`);
2. else the one the task's connection manager points at (the data flow component's, or the Execute SQL
   task's): a catalog value for a project connection manager's `InitialCatalog` or `ConnectionString`,
   else an expression on them (string literals and `$Project::`, `$Package::` and `User::` values joined
   with `+`), else its connection string;
3. a `$Project::` value comes from `Project.params` in your files; with `--env`, from the catalog: the
   value set on the deployed project, or the variable of the SSIS environment it references. When the
   project references several environments that disagree, or the value is sensitive, it is not known.

A connection whose database cannot be worked out is listed in the package's `unknown_database`; its tables
match others by `schema.table` only, and such links carry `unsure` (dotted in the diagram). Your files only
have the design values of parameters: when the deployed values differ (a parameter bound to an environment),
graph the deployed projects with `--env` to see the databases the packages really use.

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
| `--dir FOLDER` (repeat) | read these project folders too: another layer's project |
| `--also FOLDER/PROJECT` (repeat), `--all-projects` | with `--env`: read these deployed projects too, or every one in the catalog |

## The JSON

- `nodes`: each package with its `role` (`upstream`, `this`, `downstream`, or `both`), `steps` from the
  package, its `project` when several are read, and `unknown_sql`, `unknown_database` or `missing` when they
  apply; upstream first;
- `edges`: `from` runs or writes before `to`, with `kind`, the `tables` and `via` in words, e.g.
  "Stage_SalesOrder.dtsx reads stage.customer, which Stage_Customer.dtsx writes";
- `order`: a run order that respects every link, or `cycles` when packages feed each other.

It is quick: the links are indexed by table, so a project of 2000 packages takes well under a second
after its packages are read, and reading 200 packages takes about a tenth of a second. In the lab,
`--all-projects` over 27 deployed projects takes 0.6 s.

`lab verify` checks it on SQL Server 2019 with projects saved by the SSIS object model: Execute Package tasks,
a data flow and SQL from an expression; and three layers in three databases, each with a `dbo.Customer`,
whose databases come from parameters bound to SSIS environments. Left at their design value, those
parameters would merge the layers into one database (9 links, 5 of them false); read from the
environments, the graph has exactly the 4 real links.
