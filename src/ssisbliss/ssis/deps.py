"""How packages depend on each other: Execute Package calls, tables one package writes and another
reads (from Execute SQL tasks and data-flow components), and shared project parameters.

Table names are normalised to database.schema.table in lower case (dbo when no schema is given). The
database is the one the SQL names, else the one the task's connection manager points at: its connection
string, an expression on it (string literals and $Project::, $Package:: and User:: values), or the value
the catalog has for it. A database that cannot be worked out is "?": such a table may be any database's,
and a link through it says so. Read from your files, a database that comes from a project parameter or a
project connection manager is the design value, which the catalog can replace: a link through it is only
sure between packages that take it from the same parameter of the same project. Temp tables, table variables and SQL built at run time (expressions,
variables, files) are left out, and listed in `unknown_sql` so a reader knows what could not be followed.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache

from .model import Component, Package, ProjectInfo, database_of, evaluate  # noqa: F401 - used through deps
from .sqlformat import CACHED

_PACKAGE_NAME = re.compile(r"<PackageName>\s*([^<]+?)\s*</PackageName>", re.I)
_PROJECT_PARAM = re.compile(r"\$Project::(\w+)")
_WRITES = ("Insert", "Update", "Delete", "Merge", "Create", "TruncateTable", "Drop")


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
    assumed: dict[str, str] = field(default_factory=dict)  # read table -> the design value its database comes from
    assumed_writes: dict[str, str] = field(default_factory=dict)   # the same for the tables it writes

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


def _dataflow_tables(components: list[Component]) -> tuple[set[str], set[str], list[str], list[str], dict]:
    """(reads, writes, unknown_sql, unknown_db, assumed) of a data flow's sources and destinations; assumed is
    ("r" or "w", table) -> where its design-value database comes from."""
    reads, writes, unknown, unknown_db, guessed = set(), set(), [], [], {}
    for comp in components:
        cls, props = comp.cls.lower(), comp.properties
        is_dest = "destination" in cls
        db = comp.connection.database if comp.connection else None
        source = comp.connection.assumed if comp.connection else None
        before = {("r", t) for t in reads} | {("w", t) for t in writes}
        if comp.element.find("connections/connection") is not None and db is None:
            unknown_db.append(f"{comp.name} (connection {comp.connection_name})")
        # OLE DB AccessMode: source 0 table, 1 table from variable, 2 SQL, 3 SQL from variable;
        # destination 0 table, 1 from variable, 3 fast load, 4 fast load from variable.
        mode = props.get("AccessMode", "0").strip() or "0"
        table = (props.get("OpenRowset") or props.get("TableOrViewName") or "").strip()
        command = props.get("SqlCommand", "").strip()
        if mode in (("1", "3") if not is_dest else ("1", "4")) and ("oledb" in cls or "OpenRowsetVariable" in props):
            unknown.append(f"{comp.name} (table or SQL from a variable)")
        elif not is_dest and mode == "2" and command:
            r, w, ok = sql_tables(command)
            reads |= {qualified(x, db) for x in r}
            writes |= {qualified(x, db) for x in w}
            if not ok:
                unknown.append(f"{comp.name} (SQL did not parse; tables found by pattern)")
        elif table:
            name = table_name(table.split("."))
            if name:
                (writes if is_dest else reads).add(qualified(name, db))
        elif command:
            r, w, ok = sql_tables(command)
            reads |= {qualified(x, db) for x in r}
            writes |= {qualified(x, db) for x in w}
        if source:
            now = {("r", t) for t in reads} | {("w", t) for t in writes}
            guessed.update({t: source for t in now - before})
    return reads, writes, unknown, unknown_db, guessed


def facts(name: str, data: bytes | Package, project: ProjectInfo | None = None) -> Facts:
    p = data if isinstance(data, Package) else Package.parse(data, project)
    text = ET.tostring(p.root, encoding="unicode") if isinstance(data, Package) else data.decode("utf-8-sig", errors="replace")
    f = Facts(package=name, project=p.project.name if p.project else "")
    f.calls = {m if m.lower().endswith(".dtsx") else f"{m}.dtsx" for m in _PACKAGE_NAME.findall(text)}
    f.parameters = set(_PROJECT_PARAM.findall(text))
    for t in p.walk():
        if t.kind != "sql" or t.sql_data is None:
            continue
        dynamic = t.expressions.get("SqlStatementSource")
        if t.sql_source != "DirectInput" or dynamic:
            f.unknown_sql.append(f"{t.name} (SQL from {'an expression' if dynamic else t.sql_source})")
            continue
        if t.sql:
            db = t.connection.database if t.connection else None
            if t.connection is not None and db is None:
                f.unknown_db.append(f"{t.name} (connection {t.connection.name})")
            r, w, ok = sql_tables(t.sql)
            f.reads |= {qualified(x, db) for x in r}
            f.writes |= {qualified(x, db) for x in w}
            if source := t.connection.assumed if t.connection else None:
                f.assumed.update({qualified(x, db): source for x in r if x.count(".") < 2})
                f.assumed_writes.update({qualified(x, db): source for x in w if x.count(".") < 2})
            if not ok:
                f.unknown_sql.append(f"{t.name} (SQL did not parse; tables found by pattern)")
    for t in p.every_task:
        if t.components:
            r, w, unknown, unknown_db, guessed = _dataflow_tables(t.components)
            f.reads |= r
            f.writes |= w
            for (kind, table), source in guessed.items():
                (f.assumed if kind == "r" else f.assumed_writes).setdefault(table, source)
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
    assumed: tuple[str, ...] = ()  # where a design-value database came from, when the two sides differ in it

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
                # a design value matches only itself: the same parameter (or connection manager) of one project
                design = {n: (f.assumed.get(n), all_facts[w].assumed_writes.get(n)) for n, sure in tables if sure}
                design = {n: s for n, s in design.items() if any(s) and s[0] != s[1]}
                out.append(Link(w, key, "tables", tuple(n for n, _ in tables),
                                tuple(n for n, sure in tables if not sure or n in design),
                                tuple(sorted({x for s in design.values() for x in s if x}))))
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
    sure_down: dict[str, list[str]] = {}
    sure_up: dict[str, list[str]] = {}
    for x in every:
        down.setdefault(x.source, []).append(x.target)
        up.setdefault(x.target, []).append(x.source)
        if len(x.unsure) < len(x.tables) or x.kind == "runs":
            sure_down.setdefault(x.source, []).append(x.target)
            sure_up.setdefault(x.target, []).append(x.source)

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
    sure = (set(walk(sure_up)) if direction in ("both", "up") else set()) | \
        (set(walk(sure_down)) if direction in ("both", "down") else set())
    edges = [x for x in every if x.source in keep and x.target in keep]

    nodes = []
    def rank(k: str) -> int:   # upstream first (farthest first), then this package, then downstream
        return -ups[k] if k in ups and k != me else downs.get(k, 0)
    for n in sorted(keep, key=lambda k: (rank(k), k.lower())):
        f = all_facts.get(n)
        role = "this" if n == me else "both" if n in ups and n in downs else "upstream" if n in ups else "downstream"
        node = {"package": n, "role": role, "steps": min(d.get(n, 10**9) for d in (ups, downs) if n in d)}
        if n not in sure and n != me:
            node["unsure"] = "reached only through unsure links: it may not be linked at all"
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
                       "via": x.via(), **({"unsure": list(x.unsure), "why_unsure": why_unsure(x)} if x.unsure else {})}
                      for x in edges],
            **({"order": order} if not cycles else {"cycles": cycles})}


def why_unsure(x: Link) -> str:
    why = []
    if x.assumed:
        why.append(f"the database is a value in your files ({'; '.join(x.assumed)}), and the catalog can set "
                   f"another one when the project is deployed: graph the deployed projects (--env) to be sure")
    if any(t.count(".") < 2 for t in x.unsure):    # shown without its unknown database
        why.append("the same schema.table, but a database is not known (unknown_database on a package): it may "
                   "be another database's table")
    return "; ".join(why)


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
