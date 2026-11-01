"""How packages depend on each other: Execute Package calls, tables one package writes and another
reads (from Execute SQL tasks and data-flow components), and shared project parameters.

Table names are normalised to database.schema.table in lower case (dbo when no schema is given). The
database is the one the SQL names, else the one the task's connection manager points at: its connection
string, an expression on it (string literals and $Project::, $Package:: and User:: values), or the value
the catalog has for it. A database that cannot be worked out is "?": such a table may be any database's,
and a link through it says so. Temp tables, table variables and SQL built at run time (expressions,
variables, files) are left out, and listed in `unknown_sql` so a reader knows what could not be followed.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import dataclass, field
from functools import lru_cache

from .outline import flatten, outline
from .sqlformat import CACHED

_PACKAGE_NAME = re.compile(r"<PackageName>\s*([^<]+?)\s*</PackageName>", re.I)
_PROJECT_PARAM = re.compile(r"\$Project::(\w+)")
_WRITES = ("Insert", "Update", "Delete", "Merge", "Create", "TruncateTable", "Drop")


_D = "{www.microsoft.com/SqlServer/Dts}"
_S = "{www.microsoft.com/SqlServer/SSIS}"
UNKNOWN_DB = "?"


@dataclass
class Facts:
    package: str
    calls: set[str] = field(default_factory=set)       # child packages (name.dtsx)
    reads: set[str] = field(default_factory=set)       # database.schema.table (database "?" when unknown)
    writes: set[str] = field(default_factory=set)
    parameters: set[str] = field(default_factory=set)
    unknown_sql: list[str] = field(default_factory=list)   # tasks whose SQL is built at run time or did not parse
    project: str = ""                                  # set when a graph spans several projects
    unknown_db: list[str] = field(default_factory=list)    # connections whose database could not be worked out

    @property
    def key(self) -> str:
        return f"{self.project}/{self.package}" if self.project else self.package


def table_name(parts: list[str]) -> str | None:
    """schema.table, or database.schema.table when the SQL names the database, in lower case."""
    parts = [p.strip("[]\"` ") for p in parts if p is not None]
    if not parts or not parts[-1] or parts[-1].startswith(("#", "@")):
        return None
    name = parts[-1]
    schema = parts[-2] if len(parts) >= 2 and parts[-2] else "dbo"
    database = parts[-3] if len(parts) >= 3 and parts[-3] else ""
    return (f"{database}.{schema}.{name}" if database else f"{schema}.{name}").lower()


def qualified(table: str, database: str | None) -> str:
    """database.schema.table: the SQL's own database, else the connection's, else "?"."""
    return table if table.count(".") >= 2 else f"{(database or UNKNOWN_DB).lower()}.{table}"


def shown(table: str) -> str:
    return table.removeprefix(UNKNOWN_DB + ".")


def shared(reads: set[str], writes: set[str]) -> list[tuple[str, bool]]:
    """Tables in both, as (name to show, sure). The same database.schema.table is sure; when either side's
    database is unknown, the same schema.table may be the same table: a link, but not a sure one."""
    by_name: dict[str, set[str]] = {}
    for w in writes:
        db, _, rest = w.partition(".")
        by_name.setdefault(rest, set()).add(db)
    out = {}
    for r in reads:
        db, _, rest = r.partition(".")
        for wdb in by_name.get(rest, ()):
            if db == wdb and db != UNKNOWN_DB:
                out[r] = True
            elif db == wdb or UNKNOWN_DB in (db, wdb):
                out.setdefault(shown(r) if db == UNKNOWN_DB else r, False)
    return sorted(out.items())


# ------------------------------------------------------------ which database a connection points at --

@dataclass
class ProjectInfo:
    """What a package's connections can depend on outside the package: the project's parameter values and
    connection managers. A value of None is set but not known (sensitive, or an environment not readable)."""
    name: str = ""
    values: dict[str, str | None] = field(default_factory=dict)    # parameter name, lower case
    managers: list[bytes] = field(default_factory=list)            # the project's .conmgr files

    @staticmethod
    def params(data: bytes | None) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        if not data:
            return out
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return out
        for p in root.iter(f"{_S}Parameter"):
            props = {x.get(f"{_S}Name"): x.text or "" for x in p.iter(f"{_S}Property")}
            out[(p.get(f"{_S}Name") or "").lower()] = None if props.get("Sensitive") == "1" else props.get("Value", "")
        return out

    @classmethod
    def from_folder(cls, folder: Path, name: str = "") -> "ProjectInfo":
        params = folder / "Project.params"
        return cls(name, cls.params(params.read_bytes() if params.is_file() else None),
                   [f.read_bytes() for f in sorted(folder.glob("*.conmgr"))])

    @classmethod
    def from_ispac(cls, ispac, name: str = "", bindings: list[dict] | None = None,
                   environment: dict[str, str | None] | None = None) -> "ProjectInfo":
        """The deployed project: Project.params, then the catalog's values (literal, or from the referenced
        environment's variable; sensitive and unreadable ones are None)."""
        values = cls.params(ispac.parts["Project.params"][1] if "Project.params" in ispac.parts else None)
        for b in bindings or []:
            if b.get("object_type") != 20:
                continue
            key = (b.get("parameter_name") or "").lower()
            if b.get("value_type") == "R":
                values[key] = (environment or {}).get((b.get("referenced_variable_name") or "").lower())
            else:
                values[key] = None if b.get("sensitive") else b.get("value")
        managers = [ispac.parts[f][1] for f in ispac.connection_manager_files if f in ispac.parts]
        return cls(name, values, managers)


def _literal(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), text)


def evaluate(expression: str, values: dict[str, str | None]) -> str | None:
    """An SSIS expression made of string literals and @[...] references joined by +, or None when it is
    anything else or a value it needs is not known. `values` keys: "$project::x", "$package::x", "user::x"."""
    out, rest = [], expression.strip()
    while rest.startswith("(") and rest.endswith(")"):
        rest = rest[1:-1].strip()
    for m in re.finditer(r'\s*(?:"((?:[^"\\]|\\.)*)"|@\[([^\]]+)\]|(\+))\s*|(.)', rest):
        if m.group(4) is not None:
            return None
        if m.group(1) is not None:
            out.append(_literal(m.group(1)))
        elif m.group(2) is not None:
            ref = m.group(2).lower()
            ref = ref if "::" in ref else f"user::{ref}"
            v = values.get(ref)
            if v is None:
                return None
            out.append(v)
    return "".join(out)


def database_of(connection_string: str | None) -> str | None:
    m = re.search(r"(?:Initial Catalog|Database)\s*=\s*([^;]+)", connection_string or "", re.I)
    return m.group(1).strip().lower() if m else None


def _values(root: ET.Element, project: ProjectInfo | None) -> dict[str, str | None]:
    """$Project::, $Package:: and User:: values for evaluating this package's expressions."""
    vals: dict[str, str | None] = {f"$project::{k}": v for k, v in (project.values if project else {}).items()}
    for p in root.iter(f"{_D}PackageParameter"):
        value = next((x.text or "" for x in p.findall(f"{_D}Property") if x.get(f"{_D}Name") == "ParameterValue"), None)
        sensitive = p.get(f"{_D}Sensitive") in ("True", "1")
        vals[f"$package::{(p.get(f'{_D}ObjectName') or '').lower()}"] = None if sensitive else value
    for v in root.iter(f"{_D}Variable"):
        if v.get(f"{_D}EvaluateAsExpression") in ("True", "1"):
            continue
        ns = (v.get(f"{_D}Namespace") or "User").lower()
        value = v.find(f"{_D}VariableValue")
        vals[f"{ns}::{(v.get(f'{_D}ObjectName') or '').lower()}"] = value.text or "" if value is not None else None
    return vals


def connection_databases(root: ET.Element, project: ProjectInfo | None) -> dict[str, str | None]:
    """Each connection manager (by refId, DTSID and name) -> the database it points at, or None."""
    vals = _values(root, project)
    managers = [(cm, "Package") for cm in root.iter(f"{_D}ConnectionManager")
                if cm.find(f"{_D}ObjectData") is not None]
    for data in (project.managers if project else []):
        try:
            managers.append((ET.fromstring(data), "Project"))
        except ET.ParseError:
            continue
    out: dict[str, str | None] = {}
    for cm, scope in managers:
        name = cm.get(f"{_D}ObjectName") or ""
        inner = cm.find(f"{_D}ObjectData/{_D}ConnectionManager")
        exprs = {e.get(f"{_D}Name"): e.text or "" for e in cm.findall(f"{_D}PropertyExpression")}
        db: str | None = None
        decided = False
        if scope == "Project":     # the catalog can set a project connection manager's properties
            for prop in ("InitialCatalog", "ConnectionString"):
                key = f"$project::cm.{name.lower()}.{prop.lower()}"
                if key in vals:
                    v = vals[key]
                    db, decided = (v.lower() if prop == "InitialCatalog" and v else database_of(v)), True
                    break
        if not decided:
            for prop in ("InitialCatalog", "ConnectionString"):
                if prop in exprs:
                    v = evaluate(exprs[prop], vals)
                    db, decided = (v.lower() if prop == "InitialCatalog" and v else database_of(v)), True
                    break
        if not decided:
            db = database_of(inner.get(f"{_D}ConnectionString") if inner is not None else None)
        for key in (cm.get(f"{_D}refId"), cm.get(f"{_D}DTSID"), f"{scope}.ConnectionManagers[{name}]", name):
            if key:
                out.setdefault(key, db)
    return out


@lru_cache(maxsize=CACHED)
def sql_tables(sql: str) -> tuple[frozenset[str], frozenset[str], bool]:
    """(reads, writes, parsed) for a T-SQL text. Falls back to keyword patterns when it does not parse."""
    try:
        import sqlglot
        from sqlglot import exp
        trees = [t for t in sqlglot.parse(re.sub(r"(?im)^\s*GO\s*$", ";", sql), dialect="tsql") if t is not None]
    except Exception:   # noqa: BLE001 - any parser failure falls back to patterns
        return (*map(frozenset, _pattern_tables(sql)), False)
    reads, writes = set(), set()
    for tree in trees:
        aliases = {t.alias.lower(): t for t in tree.find_all(exp.Table) if t.alias}
        targets, skip = set(), set()

        def target(node):
            node = node.this if isinstance(node, exp.Schema) else node
            if isinstance(node, exp.Table):
                real = aliases.get(node.name.lower()) if not node.args.get("db") else None
                targets.add(id(real if real is not None else node))
                if real is not None:
                    skip.add(id(node))     # the alias itself is not a table

        for node in tree.walk():
            if type(node).__name__ in _WRITES or isinstance(node, exp.Into):
                target(node.args.get("this"))
            if isinstance(node, exp.TruncateTable):
                for t in node.expressions:
                    target(t)
            if type(node).__name__ == "Execute" and isinstance(node.args.get("this"), exp.Table):
                skip.add(id(node.args["this"]))      # a stored procedure, not a table
        for t in tree.find_all(exp.Table):
            if id(t) in skip or isinstance(t.this, exp.Identifier) and t.this.args.get("temporary"):
                continue
            name = table_name([t.catalog, t.db, t.name])
            if name:
                (writes if id(t) in targets else reads).add(name)
    return frozenset(reads), frozenset(writes), True


def _pattern_tables(sql: str) -> tuple[set[str], set[str]]:
    ident = r"((?:\[[^\]]+\]|[\w#@]+)(?:\.(?:\[[^\]]+\]|[\w#@]+)){0,3})"
    writes = {table_name(m.split(".")) for m in re.findall(
        rf"\b(?:INSERT\s+(?:INTO\s+)?|UPDATE\s+|DELETE\s+(?:FROM\s+)?|MERGE\s+(?:INTO\s+)?|TRUNCATE\s+TABLE\s+|INTO\s+){ident}", sql, re.I)}
    reads = {table_name(m.split(".")) for m in re.findall(rf"\b(?:FROM|JOIN)\s+{ident}", sql, re.I)}
    return {r for r in reads if r} - writes, {w for w in writes if w}


def _dataflow_tables(task_xml: ET.Element, databases: dict[str, str | None]
                     ) -> tuple[set[str], set[str], list[str], list[str]]:
    """(reads, writes, unknown_sql, unknown_db) of a data flow's sources and destinations."""
    reads, writes, unknown, unknown_db = set(), set(), [], []
    for comp in task_xml.iter("component"):
        cls = (comp.get("componentClassID") or "").lower()
        props = {p.get("name"): (p.text or "") for p in comp.iter("property")}
        is_dest = "destination" in cls
        ref = comp.find("connections/connection")
        key = (ref.get("connectionManagerRefId") or ref.get("connectionManagerID") or "") if ref is not None else ""
        key = key.removesuffix(":external")
        cm = re.sub(r".*\[(.*)\]$", r"\1", key)
        db = databases.get(key, databases.get(cm))
        if ref is not None and db is None:
            unknown_db.append(f"{comp.get('name')} (connection {cm})")
        # OLE DB AccessMode: source 0 table, 1 table from variable, 2 SQL, 3 SQL from variable;
        # destination 0 table, 1 from variable, 3 fast load, 4 fast load from variable.
        mode = props.get("AccessMode", "0").strip() or "0"
        table = (props.get("OpenRowset") or props.get("TableOrViewName") or "").strip()
        command = props.get("SqlCommand", "").strip()
        if mode in (("1", "3") if not is_dest else ("1", "4")) and ("oledb" in cls or "OpenRowsetVariable" in props):
            unknown.append(f"{comp.get('name')} (table or SQL from a variable)")
        elif not is_dest and mode == "2" and command:
            r, w, ok = sql_tables(command)
            reads |= {qualified(x, db) for x in r}
            writes |= {qualified(x, db) for x in w}
            if not ok:
                unknown.append(f"{comp.get('name')} (SQL did not parse; tables found by pattern)")
        elif table:
            name = table_name(table.split("."))
            if name:
                (writes if is_dest else reads).add(qualified(name, db))
        elif command:
            r, w, ok = sql_tables(command)
            reads |= {qualified(x, db) for x in r}
            writes |= {qualified(x, db) for x in w}
    return reads, writes, unknown, unknown_db


def facts(name: str, data: bytes, project: ProjectInfo | None = None) -> Facts:
    text = data.decode("utf-8-sig", errors="replace")
    f = Facts(package=name, project=project.name if project else "")
    f.calls = {m if m.lower().endswith(".dtsx") else f"{m}.dtsx" for m in _PACKAGE_NAME.findall(text)}
    f.parameters = set(_PROJECT_PARAM.findall(text))
    root = ET.fromstring(data)
    databases = connection_databases(root, project)
    o = outline(data)
    for t in flatten(o["tasks"]):
        if t["kind"] != "sql":
            continue
        dynamic = (t.get("expressions") or {}).get("SqlStatementSource")
        if t.get("sql_source") or dynamic:
            f.unknown_sql.append(f"{t['name']} (SQL from {'an expression' if dynamic else t['sql_source']})")
            continue
        if t.get("sql"):
            db = databases.get(t.get("connection") or "")
            if t.get("connection") in databases and db is None:
                f.unknown_db.append(f"{t['name']} (connection {t['connection']})")
            r, w, ok = sql_tables(t["sql"])
            f.reads |= {qualified(x, db) for x in r}
            f.writes |= {qualified(x, db) for x in w}
            if not ok:
                f.unknown_sql.append(f"{t['name']} (SQL did not parse; tables found by pattern)")
    for ex in root.iter():
        if ex.tag.endswith("}ObjectData") or ex.tag == "ObjectData":
            pipeline = ex.find("pipeline")
            if pipeline is not None:
                r, w, unknown, unknown_db = _dataflow_tables(pipeline, databases)
                f.reads |= r
                f.writes |= w
                f.unknown_sql += unknown
                f.unknown_db += unknown_db
    return f


@dataclass
class Edge:
    package: str     # the other package
    via: str         # why they are linked, in words


def related(target: str, all_facts: dict[str, Facts]) -> tuple[list[Edge], list[Edge]]:
    """(depends_on, used_by) for target among all_facts (keys: package names). A package depends on the
    children it runs and on the packages that write what it reads."""
    lower = {k.lower(): k for k in all_facts}
    me = lower.get(target.lower(), target)
    depends, used = [], []
    for x in links(all_facts):
        parent_or_reader, child_or_writer = (x.source, x.target) if x.kind == "runs" else (x.target, x.source)
        if parent_or_reader == me and child_or_writer != me:
            depends.append(Edge(child_or_writer, x.via()))
        elif child_or_writer == me and parent_or_reader != me:
            used.append(Edge(parent_or_reader, x.via()))
    return _merge(depends), _merge(used)


def _merge(edges: list[Edge]) -> list[Edge]:
    out: dict[str, Edge] = {}
    for e in edges:
        out[e.package] = Edge(e.package, f"{out[e.package].via}; {e.via}") if e.package in out else e
    return list(out.values())


# ------------------------------------------------------------------------------ the graph --

@dataclass(frozen=True)
class Link:
    source: str                  # runs first: the parent package, or the one that writes the tables
    target: str
    kind: str                    # "runs" (Execute Package task) or "tables"
    tables: tuple[str, ...] = ()
    unsure: tuple[str, ...] = ()  # tables matched by schema.table only: a database was not known

    def via(self) -> str:
        if self.kind == "runs":
            return f"{self.source} runs {self.target} (Execute Package task)"
        return f"{self.target} reads {', '.join(self.tables)}, which {self.source} writes"


def links(all_facts: dict[str, Facts]) -> list[Link]:
    """Every link between the packages (all_facts keys: Facts.key): Execute Package calls within a project,
    and tables one package writes and another reads, in any project. Tables are indexed by name, so this
    is linear in the tables, not quadratic in the packages."""
    in_project = {(f.project, f.package.lower()): k for k, f in all_facts.items()}
    writers: dict[str, set[str]] = {}
    for key, f in all_facts.items():
        for w in f.writes:
            writers.setdefault(w.partition(".")[2], set()).add(key)
    out = []
    for key, f in all_facts.items():
        for c in sorted(f.calls, key=str.lower):
            child = in_project.get((f.project, c.lower()), f"{f.project}/{c}" if f.project else c)
            out.append(Link(key, child, "runs"))
        candidates = {w for r in f.reads for w in writers.get(r.partition(".")[2], ()) if w != key}
        for w in sorted(candidates):
            tables = shared(f.reads, all_facts[w].writes)
            if tables:
                out.append(Link(w, key, "tables", tuple(n for n, _ in tables), tuple(n for n, sure in tables if not sure)))
    return sorted(out, key=lambda x: (x.source.lower(), x.target.lower(), x.kind))


def graph(target: str, all_facts: dict[str, Facts], depth: int | None = None, direction: str = "both") -> dict:
    """The packages `target` depends on (upstream: what runs it or writes what it reads) and that depend on it
    (downstream), followed link by link up to `depth`, with every link among them. `order` is a run order
    that respects every link when there is one; `cycles` lists the packages that feed each other."""
    names = {k.lower(): k for k in all_facts}
    me = names.get(target.lower(), target)
    every = links(all_facts)
    down: dict[str, list[str]] = {}
    up: dict[str, list[str]] = {}
    for x in every:
        down.setdefault(x.source, []).append(x.target)
        up.setdefault(x.target, []).append(x.source)

    def walk(adjacent: dict[str, list[str]]) -> dict[str, int]:
        seen, frontier, step = {me: 0}, [me], 0
        while frontier and (depth is None or step < depth):
            step += 1
            frontier = [n for f in frontier for n in adjacent.get(f, ()) if n not in seen]
            for n in frontier:
                seen.setdefault(n, step)
            frontier = list(dict.fromkeys(frontier))
        return seen

    ups = walk(up) if direction in ("both", "up") else {me: 0}
    downs = walk(down) if direction in ("both", "down") else {me: 0}
    keep = set(ups) | set(downs)
    edges = [x for x in every if x.source in keep and x.target in keep]

    nodes = []
    def rank(k: str) -> int:   # upstream first (farthest first), then this package, then downstream
        return -ups[k] if k in ups and k != me else downs.get(k, 0)
    for n in sorted(keep, key=lambda k: (rank(k), k.lower())):
        f = all_facts.get(n)
        role = "this" if n == me else "both" if n in ups and n in downs else "upstream" if n in ups else "downstream"
        node = {"package": n, "role": role, "steps": min(d.get(n, 10**9) for d in (ups, downs) if n in d)}
        if f is None:
            node["missing"] = "called by an Execute Package task but not in its project"
        else:
            if f.project:
                node["project"] = f.project
            if f.unknown_sql:
                node["unknown_sql"] = f.unknown_sql
            if f.unknown_db:
                node["unknown_database"] = f.unknown_db
        nodes.append(node)

    # Kahn: a run order when the links allow one; what is left over is in a cycle
    indegree = {n: 0 for n in keep}
    after: dict[str, list[str]] = {}
    for x in edges:
        indegree[x.target] += 1
        after.setdefault(x.source, []).append(x.target)
    ready = sorted((n for n, d in indegree.items() if d == 0), key=str.lower)
    order = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in after.get(n, ()):
            indegree[m] -= 1
            if indegree[m] == 0:
                ready.append(m)
        ready.sort(key=str.lower)
    cycles = sorted((n for n in keep if n not in order), key=str.lower)

    projects = sorted({f.project for f in all_facts.values() if f.project})
    return {"package": me, "found": me in all_facts, "packages_read": len(all_facts),
            **({"projects": projects} if projects else {}),
            "nodes": nodes,
            "edges": [{"from": x.source, "to": x.target, "kind": x.kind, **({"tables": list(x.tables)} if x.tables else {}),
                       "via": x.via(),
                       **({"unsure": list(x.unsure), "why_unsure": "the same schema.table, but a database is not "
                           "known (unknown_database on a package): it may be another database's table"}
                          if x.unsure else {})} for x in edges],
            **({"order": order} if not cycles else {"cycles": cycles})}


def _label(x: dict, most: int = 2) -> str:
    if x["kind"] == "runs":
        return "runs"
    t = x["tables"]
    return ", ".join(t[:most]) + (f" +{len(t) - most} more" if len(t) > most else "")


def mermaid(g: dict) -> str:
    """The graph as a Mermaid flowchart (GitHub, VS Code and pull requests render it)."""
    ids = {n["package"]: f"n{i}" for i, n in enumerate(g["nodes"])}
    lines = ["flowchart LR"]
    for n in g["nodes"]:
        lines.append(f'    {ids[n["package"]]}["{n["package"].replace(chr(34), "#quot;")}"]')
    for x in g["edges"]:
        arrow = "==>" if x["kind"] == "runs" else "-.->" if x.get("unsure") else "-->"
        lines.append(f'    {ids[x["from"]]} {arrow}|"{_label(x).replace(chr(34), "#quot;")}"| {ids[x["to"]]}')
    lines += ["    classDef this stroke-width:3px", "    classDef unknown stroke-dasharray: 5 5",
              "    classDef missing stroke-dasharray: 2 2,color:#888"]
    for n in g["nodes"]:
        cls = "this" if n["role"] == "this" else "missing" if n.get("missing") else "unknown" if n.get("unknown_sql") else ""
        if cls:
            lines.append(f"    class {ids[n['package']]} {cls}")
    return "\n".join(lines) + "\n"


def dot(g: dict) -> str:
    """The graph for Graphviz (dot -Tsvg)."""
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    lines = ["digraph ssis {", "    rankdir=LR;", "    node [shape=box];"]
    for n in g["nodes"]:
        style = ("penwidth=3" if n["role"] == "this" else "style=dotted" if n.get("missing")
                 else "style=dashed" if n.get("unknown_sql") else "")
        lines.append(f"    {q(n['package'])}" + (f" [{style}]" if style else "") + ";")
    for x in g["edges"]:
        style = ", style=bold" if x["kind"] == "runs" else ", style=dashed" if x.get("unsure") else ""
        lines.append(f"    {q(x['from'])} -> {q(x['to'])} [label={q(_label(x))}{style}];")
    return "\n".join(lines) + "\n}\n"
