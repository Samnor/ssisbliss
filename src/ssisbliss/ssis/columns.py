"""Data flow columns against the server's tables, before a deploy.

A data flow keeps its own copy of each source's and destination's columns (external metadata).
When a table changes after the package was built, the package fails at run time with
VS_NEEDSNEWMETADATA, a truncation error or an insert that leaves out a new required column. Visual
Studio only notices when someone opens the data flow; `ssisbliss lint --env ENV` compares every
OLE DB and ADO.NET source and destination with the tables (or the source query's result) on that
environment's server. Read-only: nothing is executed (queries are only described).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .dtsx import DTS
from .findings import Finding
from .model import Package, ProjectInfo

_D = f"{{{DTS}}}"

SOURCES = {"Microsoft.OLEDBSource", "Microsoft.ADONETSource", "Microsoft.SqlServer.Dts.Pipeline.DataReaderSourceAdapter"}
DESTINATIONS = {"Microsoft.OLEDBDestination", "Microsoft.ADONETDestination", "Microsoft.SQLServerDestination"}

# SQL Server type -> SSIS data flow type.
SQL_TO_SSIS = {"nvarchar": "wstr", "nchar": "wstr", "varchar": "str", "char": "str", "int": "i4", "bigint": "i8",
               "smallint": "i2", "tinyint": "ui1", "bit": "bool", "real": "r4", "float": "r8",
               "decimal": "numeric", "numeric": "numeric", "money": "cy", "smallmoney": "cy",
               "datetime": "dbTimeStamp", "smalldatetime": "dbTimeStamp", "datetime2": "dbTimeStamp2",
               "date": "dbDate", "time": "dbTime2", "datetimeoffset": "dbTimeStampoffset",
               "uniqueidentifier": "guid", "binary": "bytes", "varbinary": "bytes", "timestamp": "bytes",
               "rowversion": "bytes", "text": "text", "ntext": "ntext", "image": "image", "xml": "ntext",
               "sql_variant": "wstr", "hierarchyid": "bytes", "geography": "image", "geometry": "image"}
_MAX = {"wstr": "ntext", "str": "text", "bytes": "image"}
# Older providers (SQLOLEDB) describe the newer date and time types as text.
_SAME = {("wstr", "dbDate"), ("wstr", "dbTimeStamp2"), ("wstr", "dbTime2"), ("wstr", "dbTimeStampoffset"),
         ("dbTimeStamp", "dbTimeStamp2"), ("dbTimeStamp2", "dbTimeStamp")}
_SIZED = {"wstr", "str", "bytes"}


@dataclass
class Col:
    name: str
    type: str
    length: int = 0
    precision: int = 0
    scale: int = 0
    required: bool = False   # destination tables: NOT NULL, no default, not identity or computed

    def show(self) -> str:
        if self.type in _SIZED:
            return f"{self.type}({self.length})"
        if self.type == "numeric":
            return f"numeric({self.precision},{self.scale})"
        return self.type


@dataclass
class Endpoint:
    task: str
    path: str
    component: str
    role: str                    # source / destination
    connection: str
    database: str | None
    table: str | None = None
    sql: str | None = None
    dynamic: str | None = None   # why it cannot be checked (table name from a variable, ...)
    columns: list[Col] = field(default_factory=list)


def sql_column(name: str, sql_type: str, max_length: int, precision: int, scale: int, required: bool = False) -> Col:
    """A server column (sys.columns / sp_describe_first_result_set) in SSIS terms; max_length in bytes."""
    t = SQL_TO_SSIS.get(sql_type.lower(), sql_type.lower())
    if t in _SIZED and max_length == -1:
        return Col(name, _MAX[t], required=required)
    length = max_length // 2 if t == "wstr" else max_length if t in _SIZED else 0
    return Col(name, t, length, precision if t == "numeric" else 0,
               scale if t in ("numeric", "dbTimeStamp2", "dbTime2", "dbTimeStampoffset") else 0, required)


def _external(element: ET.Element | None) -> list[Col]:
    if element is None:
        return []
    return [Col(c.get("name", ""), c.get("dataType", ""), int(c.get("length") or 0), int(c.get("precision") or 0),
                int(c.get("scale") or 0)) for c in element.findall("externalMetadataColumns/externalMetadataColumn")]


def endpoints(data: bytes | Package, project_dir: Path | None = None) -> list[Endpoint]:
    """Every OLE DB / ADO.NET source and destination in the package's data flows. The database is the one
    each connection's stored connection string names (what Visual Studio last evaluated)."""
    p = data if isinstance(data, Package) else Package.parse(
        data, ProjectInfo.from_folder(project_dir) if project_dir else None)
    out = []
    for task in p.every_task:
        if task.type != "Microsoft.Pipeline" and task.element.get(f"{_D}CreationName") != "Microsoft.Pipeline":
            continue
        name, path = task.name or task.path.rsplit("\\", 1)[-1], task.path
        for comp in task.components:
            c, cls = comp.element, comp.cls
            role = "source" if cls in SOURCES else "destination" if cls in DESTINATIONS else None
            if role is None:
                continue
            conn = comp.connection_name
            database = comp.connection.stored_database if comp.connection else None
            mode = comp.prop("AccessMode") or "0"
            table = comp.prop("OpenRowset") or comp.prop("TableOrViewName")
            sql = comp.prop("SqlCommand")
            e = Endpoint(name, path, comp.name, role, conn, database)
            if role == "source":
                output = next((o for o in c.findall("outputs/output") if o.get("isErrorOut") != "true"), None)
                e.columns = _external(output)
            else:
                e.columns = _external(c.find("inputs/input"))
            # OLE DB AccessMode: source 0 table, 1 table from variable, 2 SQL, 3 SQL from variable;
            # destination 0 table, 1 from variable, 3 fast load, 4 fast load from variable. ADO.NET: 0 table, 2 SQL.
            if mode in (("1", "3") if role == "source" else ("1", "4")):
                e.dynamic = "the table or query comes from a variable"
            elif role == "source" and mode == "2":
                e.sql = sql
            else:
                e.table = table
            if not e.table and not e.sql and not e.dynamic:
                e.dynamic = "no table or query is set"
            out.append(e)
    return out


def _qualified(table: str) -> str:
    return table if table.startswith("[") or "." in table else f"[{table}]"


def compare(file: str, e: Endpoint, server: list[Col], where: str) -> list[Finding]:
    """Findings for one source or destination, given the server's columns for its table or query."""
    found = []
    by_name = {c.name.lower(): c for c in server}
    mine = {c.name.lower() for c in e.columns}
    what = f"{e.component} ({e.role}, {where})"

    def add(rule, severity, message, hint):
        found.append(Finding(rule, severity, file, e.task, e.path, 1, 1, f"{what}: {message}", hint))

    refresh = "open the data flow in Visual Studio and refresh the component's columns, then rebuild"
    for c in e.columns:
        s = by_name.get(c.name.lower())
        if s is None:
            add("SSB402", "error", f"column {c.name} is not on the server any more; the package fails with "
                "VS_NEEDSNEWMETADATA", refresh + (", or put the column back" if e.role == "destination" else ""))
        elif s.type != c.type and (c.type, s.type) not in _SAME:
            add("SSB403", "error", f"column {c.name} is {s.show()} on the server but {c.show()} in the package; the "
                "package fails with VS_NEEDSNEWMETADATA or a conversion error", refresh)
        elif c.type in _SIZED and s.length != c.length:
            # Data flows from the narrower side into the wider one is fine; the other way truncates.
            limit = c.length if e.role == "source" else s.length
            if (s.length > c.length) == (e.role == "source"):
                add("SSB404", "error", f"column {c.name} is {s.show()} on the server but {c.show()} in the package: "
                    f"values longer than {limit} fail with a truncation error",
                    refresh + (" (or widen the table's column)" if e.role == "destination" else ""))
            else:
                add("SSB404", "warn", f"column {c.name} is {s.show()} on the server but {c.show()} in the package "
                    "(out of sync; SSIS warns at validation)", refresh)
        elif c.type == "numeric" and (s.precision, s.scale) != (c.precision, c.scale):
            add("SSB404", "warn", f"column {c.name} is {s.show()} on the server but {c.show()} in the package", refresh)
    if e.role == "destination":
        for s in server:
            if s.required and s.name.lower() not in mine:
                add("SSB405", "error", f"the table has a new required column {s.name} ({s.show()}, NOT NULL, no "
                    "default) that the package does not fill; every insert fails",
                    "give the column a default, or map it in the destination and refresh the columns")
    return found


def server_message(error: str) -> str:
    """SQL Server's own messages from a driver error, without the generic ones that only say an earlier
    message is the reason (11501 "could not be analyzed because of compile errors", 11529, 8180)."""
    found = re.findall(r"\]([^\[\]]+?)\s*\((\d+)\)", error)
    real = [m.strip() for m, code in found if code not in ("11501", "11529", "8180")]
    return "; ".join(dict.fromkeys(real or [m.strip() for m, _ in found]))[:300] or error[:300]


class ServerSchema:
    """Columns of tables and query results on a SQL Server, read through a pyodbc connection."""

    def __init__(self, conn):
        self.conn = conn
        self._dbs: set[str] | None = None

    def has_database(self, name: str) -> bool:
        if self._dbs is None:
            self._dbs = {r[0].lower() for r in self.conn.cursor().execute("SELECT name FROM sys.databases")}
        return name.lower() in self._dbs

    def table(self, database: str, table: str) -> list[Col] | None:
        db = database.replace("]", "]]")
        rows = self.conn.cursor().execute(
            f"""SELECT c.name, TYPE_NAME(c.system_type_id), c.max_length, c.precision, c.scale,
                       CASE WHEN c.is_nullable = 0 AND c.default_object_id = 0 AND c.is_identity = 0
                                 AND c.is_computed = 0 AND TYPE_NAME(c.system_type_id) <> 'timestamp' THEN 1 ELSE 0 END
                FROM [{db}].sys.columns c WHERE c.object_id = OBJECT_ID(?) ORDER BY c.column_id""",
            f"[{db}].{_qualified(table)}").fetchall()
        return [sql_column(r[0], r[1], r[2], r[3], r[4], bool(r[5])) for r in rows] or None

    def query(self, database: str, sql: str) -> list[Col] | str:
        """The first result set's columns, or the server's message when it cannot describe the query."""
        n = 0

        def marker(_):
            nonlocal n
            n += 1
            return f"@__p{n}"
        text = re.sub(r"\?", marker, sql)
        declared = ", ".join(f"@__p{k} nvarchar(4000)" for k in range(1, n + 1))
        db = database.replace("]", "]]")
        try:
            rows = self.conn.cursor().execute(
                f"EXEC [{db}].sys.sp_describe_first_result_set @tsql = ?, @params = ?", text, declared or None).fetchall()
        except Exception as ex:   # noqa: BLE001 - the server's message is the finding
            return server_message(str(ex))
        out = []
        for r in rows:
            if getattr(r, "is_hidden", 0):
                continue
            base = (r.system_type_name or "").split("(")[0]
            out.append(sql_column(r.name or "", base, r.max_length, r.precision, r.scale))
        return out


def check(file: str, data: bytes, schema, server_label: str, project_dir: Path | None = None) -> list[Finding]:
    """Every data flow source and destination of one package against the server (`schema`: ServerSchema
    or anything with has_database / table / query)."""
    found = []
    try:
        eps = endpoints(data, project_dir)
    except ET.ParseError:
        return []
    for e in eps:
        def note(message):
            found.append(Finding("SSB409", "info", file, e.task, e.path, 1, 1, f"{e.component}: not checked: {message}",
                                 ""))
        if e.dynamic:
            note(e.dynamic)
            continue
        if not e.database:
            note(f"no database in connection {e.connection}")
            continue
        if not schema.has_database(e.database):
            note(f"database {e.database} (connection {e.connection}) is not on {server_label}")
            continue
        if e.table:
            cols = schema.table(e.database, e.table)
            if cols is None:
                found.append(Finding("SSB401", "error", file, e.task, e.path, 1, 1,
                                     f"{e.component} ({e.role}): table {e.table} is not in {e.database} on "
                                     f"{server_label}", "deploy the table to this environment first, or fix the "
                                     "table name"))
                continue
            found += compare(file, e, cols, f"{e.database}.{e.table}")
        else:
            cols = schema.query(e.database, e.sql or "")
            if isinstance(cols, str):
                # A query that no longer compiles fails at night the same way a missing column does.
                gone = re.search(r"Invalid (column|object) name", cols)
                if gone:
                    rule = "SSB402" if gone.group(1) == "column" else "SSB401"
                    found.append(Finding(rule, "error", file, e.task, e.path, 1, 1,
                                         f"{e.component} (source, its query in {e.database}): the query fails on "
                                         f"{server_label}: {cols}", "fix the query, or deploy the database change "
                                         "it needs to this environment first"))
                else:
                    note(f"{server_label} cannot describe its query: {cols}")
                continue
            found += compare(file, e, cols, f"its query in {e.database}")
    return found
