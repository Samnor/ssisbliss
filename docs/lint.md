# Linting packages and their SQL (`lint`, `format`, `sql-diff`)

SSIS keeps an Execute SQL task's statement in an XML attribute, with its line breaks stored as
`&#xA;`. In a pull request the whole statement is therefore one changed line, however it is laid
out. Three commands make SQL in packages reviewable:

```bash
ssisbliss lint STAGE                       # correctness and readability findings (JSON)
ssisbliss lint --changed STAGE             # only packages this branch changed
ssisbliss format STAGE/Load_Customer.dtsx  # lay out the SQL readably (--check: change nothing, exit 2 if it would)
ssisbliss sql-diff --formatted --markdown STAGE   # the SQL changes, as a section for the PR description
```

## `sql-diff`: what reviewers read

For every package that differs from where the branch left the default branch (committed or not),
each changed statement as an ordinary line-by-line diff, with the task it belongs to. `--formatted`
lays out both sides first, so only real changes show (layout-only edits disappear); `--markdown`
prints a ready section for the PR description instead of JSON. `--base REF` compares with another ref.

## `format`: one layout, safe by construction

Each major clause on its own line (`SELECT`, `FROM`, `JOIN`, `WHERE`, `GROUP BY`, `ORDER BY`, `INSERT
INTO`, `VALUES`, `SET`, `MERGE`, `USING`, `WHEN ... THEN`, `OUTPUT`), one select column or SET
assignment per line, `AND`/`OR` conditions on their own lines, indented `BEGIN ... END`, `TRY` /
`CATCH`, `IF` / `ELSE` bodies and subqueries; reserved keywords upper case.

- Only whitespace and the case of reserved keywords change. Identifiers, strings and comments are
  never touched, and before anything is written the result must have exactly the original's tokens
  in the same order; otherwise the statement is left as it is, with the reason.
- Formatting twice changes nothing, and the result does not depend on how the original was spaced.
- Only the bytes of each statement change; the rest of the file stays byte for byte. Statements are
  written the way SSIS's object model writes them (`&#xA;` line breaks, no carriage returns).
- SQL from an expression, a variable or a file is left alone.
- Verified on SQL Server 2019 (`lab.py verify`, check `format-roundtrip`): SSIS's own object model
  reads a formatted package back with exactly the laid-out SQL and the same load result as the
  original, and the formatted package deploys and runs in the catalog like the original.

## `lint`: the rules

| Rule | Severity | Finds |
|---|---|---|
| SSB101 sql-not-static | info | SQL from an expression, variable or file (not checked) |
| SSB102 parameter-markers | error | `?` markers and Parameter Mapping rows do not match; `?` on an ADO.NET connection |
| SSB103 result-set | warn / error | ResultSet set but nothing bound, or results bound with ResultSet None |
| SSB104 default-name | warn | a task still called "Execute SQL Task 1" (says nothing in a review) |
| SSB105 disabled-task | info | disabled task |
| SSB201 sql-parse | info | ssisbliss's own parser could not read the SQL (not necessarily wrong) |
| SSB202 no-where | warn | `DELETE` / `UPDATE` without `WHERE` |
| SSB203 select-star | warn | `SELECT *` (worst in data-flow sources) |
| SSB204 nolock | warn | `NOLOCK` / `READUNCOMMITTED` |
| SSB205 insert-columns | warn | `INSERT` without a column list |
| SSB206 database-name | info | a hard-coded database in a three-part name |
| SSB210 server-parse | error | with `--env ENV`: the server's own parser (`SET PARSEONLY`) rejects the SQL |
| SSB401 table-missing | error | with `--env`: a data flow's source or destination table is not on the server |
| SSB402 column-missing | error | with `--env`: a data flow column is no longer in the table or query (VS_NEEDSNEWMETADATA) |
| SSB403 column-type | error | with `--env`: a column's type differs between the package and the server |
| SSB404 column-size | warn / error | with `--env`: a column's length differs; an error when values would be truncated |
| SSB405 new-required-column | error | with `--env`: the destination table has a NOT NULL column without a default that the package does not fill |
| SSB409 columns-not-checked | info | with `--env`: a source or destination could not be compared (table from a variable, database on another server) |
| SSB501 row-by-row | warn | an OLE DB Command or Slowly Changing Dimension component runs one statement per row |
| SSB502 no-fast-load | warn | an OLE DB destination without fast load, or an ADO.NET destination with bulk insert off, inserts one row at a time |
| SSB503 lookup-no-cache | warn | a Lookup with no cache queries the server once per row |
| SSB504 blocking-sort | info | a Sort component holds every row in memory; sort in the source query instead |
| SSB510 excel-driver | warn / info | Excel or Access through Jet (32-bit only: works in Visual Studio, fails under SQL Agent) or ACE (needs the Access Database Engine on the server) |
| SSB511 provider-missing | error | with `--env`: the server does not have the Excel/Access OLE DB provider the package uses |
| SSB520 temp-table-session | warn | an Execute SQL task makes a `#temp` table on a connection without RetainSameConnection, so the next task cannot see it |
| SSB521 hard-coded-server | info | a package connection string names a server and has no expression: every environment uses it unless the catalog overrides it |
| SSB301 not-formatted | warn | the SQL is not laid out the `format` way |
| SSB302 cannot-format | info | the SQL could not be laid out automatically |
| SSB303 long-line | info | a line longer than `--max-line` (120) after layout |
| SSB304 commented-out-sql | info | commented-out SQL |

`ssisbliss lint --rules` prints the list. Each finding has the package file, the task path, a line
and column inside that statement, a message and a hint.

- `--env dev` parses every statement with that environment's SQL Server (`SET PARSEONLY`: nothing
  runs; `?` markers are declared as variables). The server decides what is valid T-SQL.
- `--env dev` also compares every OLE DB and ADO.NET source and destination in the data flows with
  the tables on that server: the columns the package was built with against `sys.columns`, and a
  query source against the result the server describes for it (`sp_describe_first_result_set`;
  nothing runs). A table changed after the package was built is what fails at night with
  VS_NEEDSNEWMETADATA, a truncation error or a failing insert; Visual Studio only notices when
  someone opens the data flow. The database comes from the connection manager (the package's, or
  the project's `.conmgr` next to it); databases that are not on that server are skipped (SSB409).
- `--env dev` also asks that server which OLE DB providers it has (`xp_enum_oledb_providers`, the
  64-bit ones SQL Agent's packages use) and reports an Excel or Access provider a package needs that
  is not there (SSB511). A login that may not list them skips the check.
- `--fail-on error|warn|info|never` sets when the exit code is 2 (default `error`).
- `--update-baseline` accepts today's findings, kept per user outside the repository; later runs
  report only new findings (and how many were baselined), so an old project can adopt the linter
  without fixing everything first. `--no-baseline` shows everything.
- `--ignore SSB203,SSB304` skips rules; `--max-line N` sets the line limit.
