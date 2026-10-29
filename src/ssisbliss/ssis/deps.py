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

from .outline import flatten, outline

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


def sql_tables(sql: str) -> tuple[set[str], set[str], bool]:
    """(reads, writes, parsed) for a T-SQL text. Falls back to keyword patterns when it does not parse."""
    try:
        import sqlglot
        from sqlglot import exp
        trees = [t for t in sqlglot.parse(re.sub(r"(?im)^\s*GO\s*$", ";", sql), dialect="tsql") if t is not None]
    except Exception:   # noqa: BLE001 - any parser failure falls back to patterns
        return (*_pattern_tables(sql), False)
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
    return reads, writes, True


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
