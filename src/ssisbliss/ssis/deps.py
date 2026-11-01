"""How packages depend on each other: Execute Package calls, tables one package writes and another
reads (from Execute SQL tasks and data-flow components), and shared project parameters.

Table names are normalised to schema.table in lower case (dbo when no schema is given); temp tables,
table variables and SQL built at run time (expressions, variables, files) are left out, and listed
in `unknown_sql` so a reader knows what could not be followed.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache

from .outline import flatten, outline
from .sqlformat import CACHED

_PACKAGE_NAME = re.compile(r"<PackageName>\s*([^<]+?)\s*</PackageName>", re.I)
_PROJECT_PARAM = re.compile(r"\$Project::(\w+)")
_WRITES = ("Insert", "Update", "Delete", "Merge", "Create", "TruncateTable", "Drop")


@dataclass
class Facts:
    package: str
    calls: set[str] = field(default_factory=set)       # child packages (name.dtsx)
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    parameters: set[str] = field(default_factory=set)
    unknown_sql: list[str] = field(default_factory=list)   # tasks whose SQL is built at run time or did not parse


def table_name(parts: list[str]) -> str | None:
    parts = [p.strip("[]\"` ") for p in parts if p]
    if not parts or parts[-1].startswith(("#", "@")):
        return None
    name = parts[-1]
    schema = parts[-2] if len(parts) >= 2 and parts[-2] else "dbo"
    return f"{schema}.{name}".lower()


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
            name = table_name([t.args.get("db") and t.args["db"].name, t.name])
            if name:
                (writes if id(t) in targets else reads).add(name)
    return frozenset(reads), frozenset(writes), True


def _pattern_tables(sql: str) -> tuple[set[str], set[str]]:
    ident = r"((?:\[[^\]]+\]|[\w#@]+)(?:\.(?:\[[^\]]+\]|[\w#@]+)){0,3})"
    writes = {table_name(m.split(".")) for m in re.findall(
        rf"\b(?:INSERT\s+(?:INTO\s+)?|UPDATE\s+|DELETE\s+(?:FROM\s+)?|MERGE\s+(?:INTO\s+)?|TRUNCATE\s+TABLE\s+|INTO\s+){ident}", sql, re.I)}
    reads = {table_name(m.split(".")) for m in re.findall(rf"\b(?:FROM|JOIN)\s+{ident}", sql, re.I)}
    return {r for r in reads if r} - writes, {w for w in writes if w}


def _dataflow_tables(task_xml: ET.Element) -> tuple[set[str], set[str], list[str]]:
    reads, writes, unknown = set(), set(), []
    for comp in task_xml.iter("component"):
        cls = (comp.get("componentClassID") or "").lower()
        props = {p.get("name"): (p.text or "") for p in comp.iter("property")}
        is_dest = "destination" in cls
        table = props.get("OpenRowset", "").strip()
        command = props.get("SqlCommand", "").strip()
        if table and not command:
            name = table_name(table.split("."))
            if name:
                (writes if is_dest else reads).add(name)
        elif command:
            r, w, ok = sql_tables(command)
            reads |= r
            writes |= w
            if not ok:
                unknown.append(f"{comp.get('name')} (SQL did not parse; tables found by pattern)")
        elif props.get("OpenRowsetVariable") or props.get("SqlCommandVariable"):
            unknown.append(f"{comp.get('name')} (table or SQL from a variable)")
    return reads, writes, unknown


def facts(name: str, data: bytes) -> Facts:
    text = data.decode("utf-8-sig", errors="replace")
    f = Facts(package=name)
    f.calls = {m if m.lower().endswith(".dtsx") else f"{m}.dtsx" for m in _PACKAGE_NAME.findall(text)}
    f.parameters = set(_PROJECT_PARAM.findall(text))
    o = outline(data)
    for t in flatten(o["tasks"]):
        if t["kind"] != "sql":
            continue
        dynamic = (t.get("expressions") or {}).get("SqlStatementSource")
        if t.get("sql_source") or dynamic:
            f.unknown_sql.append(f"{t['name']} (SQL from {'an expression' if dynamic else t['sql_source']})")
            continue
        if t.get("sql"):
            r, w, ok = sql_tables(t["sql"])
            f.reads |= r
            f.writes |= w
            if not ok:
                f.unknown_sql.append(f"{t['name']} (SQL did not parse; tables found by pattern)")
    root = ET.fromstring(data)
    for ex in root.iter():
        if ex.tag.endswith("}ObjectData") or ex.tag == "ObjectData":
            pipeline = ex.find("pipeline")
            if pipeline is not None:
                r, w, unknown = _dataflow_tables(pipeline)
                f.reads |= r
                f.writes |= w
                f.unknown_sql += unknown
    return f


@dataclass
class Edge:
    package: str     # the other package
    via: str         # why they are linked, in words


def related(target: str, all_facts: dict[str, Facts]) -> tuple[list[Edge], list[Edge]]:
    """(depends_on, used_by) for target among all_facts (keys: package names)."""
    lower = {k.lower(): k for k in all_facts}
    me = all_facts.get(lower.get(target.lower(), ""), Facts(target))
    depends, used = [], []
    for name, f in sorted(all_facts.items(), key=lambda kv: kv[0].lower()):
        if name.lower() == target.lower():
            continue
        if name.lower() in {c.lower() for c in me.calls}:
            depends.append(Edge(name, f"{target} runs {name} (Execute Package task)"))
        if target.lower() in {c.lower() for c in f.calls}:
            used.append(Edge(name, f"{name} runs {target} (Execute Package task)"))
        upstream = sorted(me.reads & f.writes)
        if upstream:
            depends.append(Edge(name, f"{target} reads {', '.join(upstream)}, which {name} writes"))
        downstream = sorted(me.writes & f.reads)
        if downstream:
            used.append(Edge(name, f"{name} reads {', '.join(downstream)}, which {target} writes"))
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

    def via(self) -> str:
        if self.kind == "runs":
            return f"{self.source} runs {self.target} (Execute Package task)"
        return f"{self.target} reads {', '.join(self.tables)}, which {self.source} writes"


def links(all_facts: dict[str, Facts]) -> list[Link]:
    """Every link between the packages: Execute Package calls, and tables one writes and another reads.
    Tables are indexed by writer, so this is linear in the tables, not quadratic in the packages."""
    names = {k.lower(): k for k in all_facts}
    writers: dict[str, set[str]] = {}
    for name, f in all_facts.items():
        for t in f.writes:
            writers.setdefault(t, set()).add(name)
    out = []
    for name, f in all_facts.items():
        for c in sorted(f.calls, key=str.lower):
            out.append(Link(name, names.get(c.lower(), c), "runs"))
        shared: dict[str, set[str]] = {}
        for t in f.reads:
            for w in writers.get(t, ()):
                if w != name:
                    shared.setdefault(w, set()).add(t)
        out += [Link(w, name, "tables", tuple(sorted(ts))) for w, ts in shared.items()]
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
            node["missing"] = "called by an Execute Package task but not in this project"
        elif f.unknown_sql:
            node["unknown_sql"] = f.unknown_sql
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

    return {"package": me, "found": me in all_facts, "packages_in_project": len(all_facts),
            "nodes": nodes,
            "edges": [{"from": x.source, "to": x.target, "kind": x.kind, **({"tables": list(x.tables)} if x.tables else {}),
                       "via": x.via()} for x in edges],
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
        arrow = "==>" if x["kind"] == "runs" else "-->"
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
        style = ", style=bold" if x["kind"] == "runs" else ""
        lines.append(f"    {q(x['from'])} -> {q(x['to'])} [label={q(_label(x))}{style}];")
    return "\n".join(lines) + "\n}\n"
