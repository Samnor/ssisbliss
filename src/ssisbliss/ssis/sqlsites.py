"""Where the SQL in a .dtsx lives, decoded, with the exact bytes it occupies.

Execute SQL tasks keep their statement in the SqlStatementSource attribute; SSIS's object model
writes line breaks there as &#xA;, tabs as &#x9;, and drops carriage returns (verified on SQL
Server 2019's object model). Data-flow components keep SQL in <property name="SqlCommand"> (sources,
OLE DB Command) or "SqlCommandParam" (lookups) element text. A site is one such piece of SQL: its
task path, the decoded text, and the byte span of its encoded form, so a formatter can replace
exactly those bytes and leave the rest of the file as it was.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

DTS = "{www.microsoft.com/SqlServer/Dts}"
SQLTASK = "{www.microsoft.com/sqlserver/dts/tasks/sqltask}"
_ATTR = re.compile(rb'SQLTask:SqlStatementSource="([^"]*)"')
_PROP = re.compile(rb'(<property\b[^>]*\bname="(SqlCommand|SqlCommandParam)"[^>]*>)([^<]*)(</property>)')


@dataclass
class Site:
    path: str                 # e.g. Package\Load customers, or Package\Flow\Source
    task: str                 # the task (or component) name
    kind: str                 # execute_sql | dataflow
    sql: str                  # decoded, line breaks as \n
    start: int                # byte span of the encoded value in the file
    end: int
    encoding: str             # "attribute" or "text" (element content)
    literal_newlines: bool = False     # element text written with literal line breaks
    dynamic: str = ""         # why the stored SQL is not what runs: expression, variable, file
    connection_type: str = "" # e.g. OLEDB, ADO.NET, ODBC (Execute SQL tasks)
    parameters: int = 0       # parameter bindings (Execute SQL tasks)
    result_type: str = ""     # ResultSetType_None / _SingleRow / _Rowset / _XML
    result_bindings: int = 0
    disabled: bool = False
    extra: dict = field(default_factory=dict)


def decode_attribute(raw: bytes) -> str:
    return (ET.fromstring(b'<x a="' + raw + b'"/>').get("a") or "").replace("\r\n", "\n").replace("\r", "\n")


def decode_text(raw: bytes) -> str:
    return (ET.fromstring(b"<x>" + raw + b"</x>").text or "").replace("\r\n", "\n").replace("\r", "\n")


def encode_attribute(sql: str) -> bytes:
    """As SSIS's object model writes an attribute value."""
    s = sql.replace("\r\n", "\n").replace("\r", "")
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    s = s.replace("\n", "&#xA;").replace("\t", "&#x9;")
    return s.encode("utf-8")


def encode_text(sql: str, literal_newlines: bool) -> bytes:
    s = sql.replace("\r\n", "\n").replace("\r", "")
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if not literal_newlines:
        s = s.replace("\n", "&#xA;")
    return s.encode("utf-8")


def _connections(root: ET.Element) -> dict[str, str]:
    """DTSID -> connection type (OLEDB, ADO.NET, ODBC, ...)."""
    out = {}
    for cm in root.iter(f"{DTS}ConnectionManager"):
        dtsid = cm.get(f"{DTS}DTSID")
        if dtsid:
            out[dtsid] = (cm.get(f"{DTS}CreationName") or "").split(":")[0].upper()
    return out


def _executables(root: ET.Element) -> list[tuple[ET.Element, str]]:
    """Every executable in document order with its path."""
    out = []
    for e in root.iter(f"{DTS}Executable"):
        out.append((e, e.get(f"{DTS}refId") or e.get(f"{DTS}ObjectName") or ""))
    return out


def sites(data: bytes) -> list[Site]:
    root = ET.fromstring(data)
    conns = _connections(root)
    found: list[Site] = []
    sql_tasks = []
    for e, path in _executables(root):
        if e.get(f"{DTS}ExecutableType", "").lower() == "microsoft.executesqltask":
            sql_tasks.append((e, path))
    attrs = list(_ATTR.finditer(data))
    # Each Execute SQL task has one SqlTaskData with the attribute; they appear in the same document order.
    with_attr = []
    for e, path in sql_tasks:
        td = _task_data(e)
        if td is not None and td.get(f"{SQLTASK}SqlStatementSource") is not None:
            with_attr.append((e, path, td))
    for (e, path, td), m in zip(with_attr, attrs):
        expressions = {x.get(f"{DTS}Name") for x in e.findall(f"{DTS}PropertyExpression")}
        source_type = td.get(f"{SQLTASK}SqlStmtSourceType") or "DirectInput"
        dynamic = ("expression" if "SqlStatementSource" in expressions else
                   "" if source_type == "DirectInput" else source_type)
        found.append(Site(
            path=path, task=e.get(f"{DTS}ObjectName") or path.rsplit("\\", 1)[-1], kind="execute_sql",
            sql=decode_attribute(m.group(1)), start=m.start(1), end=m.end(1), encoding="attribute", dynamic=dynamic,
            connection_type=conns.get(td.get(f"{SQLTASK}Connection") or "", ""),
            parameters=len(td.findall(f"{SQLTASK}ParameterBinding")),
            result_type=td.get(f"{SQLTASK}ResultType") or "ResultSetType_None",
            result_bindings=len(td.findall(f"{SQLTASK}ResultBinding")),
            disabled=e.get(f"{DTS}Disabled") == "True"))
    # Data-flow SQL: components in document order, their SqlCommand / SqlCommandParam properties.
    props = list(_PROP.finditer(data))
    comps = [(c, p) for c in root.iter("component") for p in c.iter("property") if p.get("name") in ("SqlCommand", "SqlCommandParam")]
    for (comp, _), m in zip(comps, props):
        raw = m.group(3)
        sql = decode_text(raw)
        if not sql.strip():
            continue
        found.append(Site(
            path=comp.get("refId") or comp.get("name") or "", task=comp.get("name") or "", kind="dataflow",
            sql=sql, start=m.start(3), end=m.end(3), encoding="text", literal_newlines=b"\n" in raw,
            extra={"property": m.group(2).decode(), "component": comp.get("componentClassID") or ""}))
    return found


def _task_data(e: ET.Element) -> ET.Element | None:
    data = e.find(f"{DTS}ObjectData")
    return data.find(f"{SQLTASK}SqlTaskData") if data is not None else None


def replace(data: bytes, changes: dict[int, str], all_sites: list[Site]) -> bytes:
    """data with the SQL of the sites at the given indexes replaced (encoded like the original)."""
    out, pos = [], 0
    for i, s in sorted(((i, all_sites[i]) for i in changes), key=lambda x: x[1].start):
        enc = encode_attribute(changes[i]) if s.encoding == "attribute" else encode_text(changes[i], s.literal_newlines)
        out += [data[pos:s.start], enc]
        pos = s.end
    out.append(data[pos:])
    return b"".join(out)
