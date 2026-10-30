"""Lint SSIS packages and the SQL in them, for correctness and for SQL you can read in a pull request.

Every finding has a stable rule id, a severity (error, warn, info), the package file, the task
(or data-flow component) path, a line and column inside that SQL, a message and a hint saying what
to do. Rules never depend on a server; `server_parse` checks SQL with the target server's own parser
when asked. One package or statement that cannot be read gives a finding, never a crash.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

import sqlglot
from sqlglot import exp

from .outline import flatten, outline
from .sqlformat import format_sql, tokenize
from .sqlsites import Site, sites

RULES = {
    # package and task rules
    "SSB101": ("sql-not-static", "info", "SQL comes from an expression, variable or file, so it is not checked"),
    "SSB102": ("parameter-markers", "error", "? parameter markers and parameter mappings do not match"),
    "SSB103": ("result-set", "warn", "result set setting and result bindings do not match"),
    "SSB104": ("default-name", "warn", "task still has its default name, which says nothing in a review"),
    "SSB105": ("disabled-task", "info", "task is disabled"),
    # SQL correctness
    "SSB201": ("sql-parse", "info", "ssisbliss's own T-SQL parser could not read it (not necessarily wrong)"),
    "SSB202": ("no-where", "warn", "DELETE or UPDATE without a WHERE clause"),
    "SSB203": ("select-star", "warn", "SELECT * (column changes break data flows and hide what is used)"),
    "SSB204": ("nolock", "warn", "NOLOCK / READUNCOMMITTED hint (dirty reads)"),
    "SSB205": ("insert-columns", "warn", "INSERT without a column list"),
    "SSB206": ("database-name", "info", "three-part name with a hard-coded database"),
    "SSB210": ("server-parse", "error", "the server's parser rejects this SQL"),
    # data flow columns against the server's tables (lint --env)
    "SSB401": ("table-missing", "error", "a data flow's table is not on the server"),
    "SSB402": ("column-missing", "error", "a data flow column is not in the table any more (VS_NEEDSNEWMETADATA)"),
    "SSB403": ("column-type", "error", "a data flow column has another type than the table's"),
    "SSB404": ("column-size", "warn", "a data flow column's length differs from the table's (error when it truncates)"),
    "SSB405": ("new-required-column", "error", "the destination table has a NOT NULL column the package does not fill"),
    "SSB409": ("columns-not-checked", "info", "a data flow source or destination could not be compared with the server"),
    # readability in a pull request
    "SSB301": ("not-formatted", "warn", "SQL is not laid out the readable way (`ssisbliss format`)"),
    "SSB302": ("cannot-format", "info", "SQL could not be laid out automatically"),
    "SSB303": ("long-line", "info", "a line longer than the limit even after layout"),
    "SSB304": ("commented-out-sql", "info", "commented-out SQL"),
}
SEVERITY = {"error": 3, "warn": 2, "info": 1}
_DEFAULT_NAME = re.compile(r"^(Execute SQL Task|Data Flow Task|Sequence Container|Script Task|Foreach Loop Container|"
                           r"For Loop Container|Execute Package Task|Execute Process Task|File System Task|Send Mail Task|"
                           r"Expression Task|OLE DB Source|OLE DB Destination|OLE DB Command|Lookup|Derived Column|"
                           r"Conditional Split|Data Conversion|Flat File Source|Flat File Destination)( \d+)?$", re.I)
_SQL_WORDS = re.compile(r"\b(SELECT|FROM|WHERE|INSERT|UPDATE|DELETE|JOIN|EXEC|MERGE)\b", re.I)


@dataclass
class Finding:
    rule: str
    severity: str
    file: str
    task: str
    path: str
    line: int
    column: int
    message: str
    hint: str = ""

    @property
    def name(self) -> str:
        return RULES[self.rule][0]

    def to_dict(self) -> dict:
        return {"rule": self.rule, "name": self.name, **{k: v for k, v in asdict(self).items() if k != "rule"}}

    def fingerprint(self) -> str:
        """Identifies a finding across edits that only move lines (for baselines)."""
        message = re.sub(r"line \d+", "", self.message)
        return f"{self.rule}|{self.file.lower()}|{self.path}|{message}"


def _pos(sql: str, offset: int) -> tuple[int, int]:
    line = sql.count("\n", 0, offset) + 1
    return line, offset - (sql.rfind("\n", 0, offset) + 1) + 1


def _f(rule: str, file: str, site_or_task, line: int, col: int, message: str, hint: str = "") -> Finding:
    task = site_or_task.task if isinstance(site_or_task, Site) else site_or_task.get("name", "")
    path = site_or_task.path if isinstance(site_or_task, Site) else site_or_task.get("path", "")
    return Finding(rule, RULES[rule][1], file, task or "", path or "", line, col, message, hint)


def lint_sql(file: str, site: Site, max_line: int = 120) -> list[Finding]:
    out: list[Finding] = []
    sql = site.sql
    tokens = tokenize(sql)
    markers = [t for t in tokens if t.kind == "char" and t.text == "?"]

    if site.kind == "execute_sql" and not site.dynamic:
        conn = site.connection_type
        if conn.startswith("ADO.NET") and markers:
            line, col = _pos(sql, markers[0].start)
            out.append(_f("SSB102", file, site, line, col, "ADO.NET connections take named parameters (@Name), not ?",
                          "use @Name in the SQL and the same names in Parameter Mapping"))
        elif conn in ("OLEDB", "ODBC", "EXCEL", "") and len(markers) != site.parameters:
            line, col = _pos(sql, markers[0].start) if markers else (1, 1)
            out.append(_f("SSB102", file, site, line, col,
                          f"{len(markers)} ? marker(s) but {site.parameters} parameter mapping(s)",
                          "one Parameter Mapping per ?, in order (names 0, 1, ...)"))
        if site.result_type in ("ResultSetType_SingleRow", "ResultSetType_Rowset", "ResultSetType_XML") and not site.result_bindings:
            out.append(_f("SSB103", file, site, 1, 1, f"ResultSet is {site.result_type.split('_')[1]} but nothing is bound",
                          "set ResultSet to None, or map the result to a variable"))
        elif site.result_type == "ResultSetType_None" and site.result_bindings:
            out.append(Finding("SSB103", "error", file, site.task, site.path, 1, 1,
                               "results are bound to variables but ResultSet is None", "set ResultSet to SingleRow or Full"))

    # SQL correctness, by sqlglot
    try:
        trees = [t for t in sqlglot.parse(re.sub(r"(?im)^\s*GO(\s+\d+)?\s*$", ";", sql), dialect="tsql") if t is not None]
    except Exception as e:   # noqa: BLE001
        errs = getattr(e, "errors", None) or [{}]
        line, col = errs[0].get("line") or 1, errs[0].get("col") or 1
        msg = str(e).splitlines()[0][:160]
        out.append(_f("SSB201", file, site, line, col, f"ssisbliss's parser could not read it ({msg}); the SQL may well "
                      "be valid, and the table and readability rules still ran", "the server's parser decides: `ssisbliss lint --env ENV`"))
        trees = []
    for tree in trees:
        for node in tree.walk():
            if isinstance(node, (exp.Delete, exp.Update)) and not node.args.get("where"):
                line, col = _first(sql, r"\b(DELETE|UPDATE)\b")
                out.append(_f("SSB202", file, site, line, col, f"{type(node).__name__.upper()} without WHERE affects every row",
                              "add a WHERE clause, or use TRUNCATE TABLE if emptying the table is the point"))
            if isinstance(node, exp.Select) and not isinstance(node.parent, exp.Exists) and any(
                    isinstance(e, exp.Star) or isinstance(e, exp.Column) and isinstance(e.this, exp.Star)
                    for e in node.expressions):
                line, col = _first(sql, r"\*")
                out.append(_f("SSB203", file, site, line, col, "SELECT * " + ("in a data-flow source: a column change "
                              "breaks the flow's metadata" if site.kind == "dataflow" else "hides which columns are used"),
                              "list the columns"))
            if isinstance(node, exp.Insert) and isinstance(node.this, exp.Table) and node.args.get("expression") is not None:
                line, col = _first(sql, r"\bINSERT\b")
                out.append(_f("SSB205", file, site, line, col, "INSERT without a column list depends on the table's column order",
                              "name the columns: INSERT INTO t (a, b) ..."))
            if isinstance(node, exp.Table) and node.args.get("catalog"):
                line, col = _first(sql, re.escape(node.args["catalog"].name))
                out.append(_f("SSB206", file, site, line, col, f"database {node.args['catalog'].name} is hard-coded",
                              "use the connection's database (or a project parameter) so the package works in every environment"))
    for t in tokens:
        if t.kind == "word" and t.upper in ("NOLOCK", "READUNCOMMITTED"):
            line, col = _pos(sql, t.start)
            out.append(_f("SSB204", file, site, line, col, f"{t.upper} reads uncommitted (possibly wrong) rows",
                          "remove the hint unless dirty reads are acceptable here"))
        if t.kind in ("line", "block") and len(set(m.upper() for m in _SQL_WORDS.findall(t.text))) >= 2:
            line, col = _pos(sql, t.start)
            out.append(_f("SSB304", file, site, line, col, "commented-out SQL", "delete it: git keeps the history"))

    # readability
    formatted, why = format_sql(sql)
    if why:
        out.append(_f("SSB302", file, site, 1, 1, f"not laid out automatically: {why}"))
    elif formatted != sql.strip("\n") and formatted != sql:
        out.append(_f("SSB301", file, site, 1, 1, f"would be laid out differently ({sql.count(chr(10)) + 1} -> "
                      f"{formatted.count(chr(10)) + 1} lines)", "run `ssisbliss format` on the package"))
    for n, line in enumerate((formatted if not why else sql).split("\n"), 1):
        if len(line) > max_line:
            out.append(_f("SSB303", file, site, n, max_line + 1, f"line {n} is {len(line)} characters (limit {max_line})",
                          "split long expressions or lists by hand"))
    return out


def _first(sql: str, pattern: str) -> tuple[int, int]:
    m = re.search(pattern, sql, re.I)
    return _pos(sql, m.start()) if m else (1, 1)


def lint_package(file: str, data: bytes, max_line: int = 120) -> list[Finding]:
    out: list[Finding] = []
    try:
        found = sites(data)
        tasks = flatten(outline(data)["tasks"])
    except Exception as e:   # noqa: BLE001 - an unreadable package is a finding, not a crash
        return [Finding("SSB201", "warn", file, "", "", 1, 1, f"package could not be read: {type(e).__name__}: {e}")]
    for t in tasks:
        if t.get("name") and _DEFAULT_NAME.match(t["name"]):
            out.append(_f("SSB104", file, t, 1, 1, f"task is still called \"{t['name']}\"",
                          "name it after what it does, e.g. \"Load stage.Customer\""))
        if t.get("disabled"):
            out.append(_f("SSB105", file, t, 1, 1, "task is disabled", "remove it if it is no longer needed"))
    for s in found:
        if s.dynamic:
            out.append(_f("SSB101", file, s, 1, 1, f"SQL comes from {'an expression' if s.dynamic == 'expression' else s.dynamic}; "
                          "the stored statement may not be what runs"))
            continue
        try:
            out += lint_sql(file, s, max_line)
        except Exception as e:   # noqa: BLE001
            out.append(_f("SSB201", file, s, 1, 1, f"could not be checked: {type(e).__name__}"))
    return sorted(out, key=lambda f: (f.file.lower(), f.path, f.line, f.column, f.rule))


def server_parse(conn, sql: str) -> str | None:
    """The server's own parser (SET PARSEONLY): None when the server accepts the SQL, else its message.
    ? markers become variables; GO separates batches. Nothing is executed."""
    out, i, n = [], 0, 0
    for t in tokenize(sql):
        out.append(sql[i:t.start])
        if t.kind == "char" and t.text == "?":
            n += 1
            out.append(f"@__p{n}")
        else:
            out.append(t.text)
        i = t.start + len(t.text)
    out.append(sql[i:])
    declare = f"DECLARE {', '.join(f'@__p{k} sql_variant' for k in range(1, n + 1))};\n" if n else ""
    batches = [declare + b for b in re.split(r"(?im)^\s*GO(?:\s+\d+)?\s*$", "".join(out)) if b.strip()]
    cur = conn.cursor()
    try:
        cur.execute("SET PARSEONLY ON")
        for b in batches:
            try:
                cur.execute(b)
            except Exception as e:   # noqa: BLE001 - the server's message is the finding
                return str(e).split("]")[-1].split("(SQLExecDirectW)")[0].strip()[:300]
        return None
    finally:
        try:
            cur.execute("SET PARSEONLY OFF")
        except Exception:   # noqa: BLE001
            pass
