"""A package read once: its connection managers, with the database each one points at, its tasks in run
order and its data flow components. outline, lint, the design rules, the data flow column checks and deps
all read packages through this model, so what one learns (a connection's database, a task's SQL) the others
see the same way.

A connection's database is, in this order: the value the catalog has for a project connection manager's
InitialCatalog or ConnectionString; else an expression on them, evaluated over string literals and the
$Project::, $Package:: and User:: values; else its connection string. Project parameter values come from
ProjectInfo: Project.params in your files, or the deployed project's values and environment variables. Read
from your files, a database that comes from a project parameter or a project connection manager is the
design value, which the catalog can replace; the connection says where it came from (`assumed`).
"""
from __future__ import annotations

import ntpath
import re
import shlex
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .dtsx import DTS

_D = f"{{{DTS}}}"
_S = "{www.microsoft.com/SqlServer/SSIS}"
SQLTASK = "{www.microsoft.com/sqlserver/dts/tasks/sqltask}"
_PROJECT_PARAM = re.compile(r"\$Project::(\w+)")

KINDS = {
    "Microsoft.ExecuteSQLTask": "sql",
    "Microsoft.ExecuteProcess": "process",
    "Microsoft.Pipeline": "dataflow",
    "Microsoft.ScriptTask": "script",
    "STOCK:SEQUENCE": "sequence",
    "STOCK:FOREACHLOOP": "foreach",
    "STOCK:FORLOOP": "for",
    "Microsoft.ExecutePackageTask": "execute_package",
    "Microsoft.FileSystemTask": "file_system",
}
CONTAINERS = ("sequence", "foreach", "for")
# Precedence constraint DTS:Value: absent/0 = success, 1 = failure, 2 = completion.
_ON = {"0": "success", "1": "failure", "2": "completion"}


# ------------------------------------------------------------------ what the project gives a package --

@dataclass
class ProjectInfo:
    """What a package's connections can depend on outside the package: the project's parameter values and
    connection managers. A value of None is set but not known (sensitive, or an environment not readable)."""
    name: str = ""
    values: dict[str, str | None] = field(default_factory=dict)    # parameter name, lower case
    managers: list[bytes] = field(default_factory=list)            # the project's .conmgr files
    design: bool = False      # your files: the catalog may set other parameter and connection manager values

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
                   [f.read_bytes() for f in sorted(folder.glob("*.conmgr"))], design=True)

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


def database_in(connection_string: str | None) -> str | None:
    """The Initial Catalog (or Database) a connection string names, as written."""
    m = re.search(r"(?:Initial Catalog|Database)\s*=\s*([^;]+)", connection_string or "", re.I)
    return m.group(1).strip() if m else None


def database_of(connection_string: str | None) -> str | None:
    db = database_in(connection_string)
    return db.lower() if db else None


# ------------------------------------------------------------------------------------ the model --

@dataclass
class Connection:
    name: str
    kind: str                     # CreationName: OLEDB, ADO.NET:..., FLATFILE, EXCEL, ...
    scope: str                    # "Package" or "Project"
    dtsid: str
    ref_id: str
    connection_string: str        # as stored (the static value; an expression may replace it at run time)
    expressions: dict[str, str]   # property -> expression
    retain: bool                  # RetainSameConnection
    database: str | None = None   # what it points at, in lower case (see the module docstring); None: not known
    assumed: str | None = None    # read from your files: the design value the database comes from

    @property
    def stored_database(self) -> str | None:
        """The database its stored connection string names, as written."""
        return database_in(self.connection_string)

    @property
    def type(self) -> str:
        """OLEDB, ADO.NET, ODBC, ... (CreationName without its provider details)."""
        return self.kind.split(":")[0].upper()


@dataclass
class Component:
    """A data flow component: a source, destination or transformation."""
    name: str
    ref_id: str
    cls: str                       # componentClassID
    properties: dict[str, str]
    connection_ref: str            # connectionManagerRefId (or ID) of its first connection, "" when none
    connection: Connection | None
    element: ET.Element

    def prop(self, name: str) -> str | None:
        return self.properties.get(name)

    @property
    def connection_name(self) -> str:
        return self.connection.name if self.connection else re.sub(r".*\[(.*)\]$", r"\1", self.connection_ref)


@dataclass
class Task:
    name: str
    path: str                      # refId, e.g. Package\Load\Copy
    type: str                      # ExecutableType
    kind: str                      # sql, dataflow, process, sequence, ... (KINDS), other
    element: ET.Element
    disabled: bool = False
    expressions: dict[str, str] = field(default_factory=dict)
    runs_after: list[dict] = field(default_factory=list)
    tasks: list["Task"] = field(default_factory=list)          # a container's own tasks, in run order
    # Execute SQL tasks
    sql_data: ET.Element | None = None
    connection_ref: str = ""
    connection: Connection | None = None
    # data flows
    components: list[Component] = field(default_factory=list)

    @property
    def sql(self) -> str | None:
        return self.sql_data.get(f"{SQLTASK}SqlStatementSource") if self.sql_data is not None else None

    @property
    def sql_source(self) -> str:
        """DirectInput, or where the statement really is (FileConnection, Variable)."""
        return (self.sql_data.get(f"{SQLTASK}SqlStmtSourceType") if self.sql_data is not None else None) or "DirectInput"

    @property
    def is_sql(self) -> bool:
        return self.sql_data is not None

    @property
    def is_dataflow(self) -> bool:
        return self.kind == "dataflow" or bool(self.components)


@dataclass
class Package:
    name: str
    root: ET.Element
    project: ProjectInfo | None
    connections: list[Connection]
    tasks: list[Task]               # the package's own tasks in run order; containers hold theirs
    every_task: list[Task]          # all tasks in document order, event handlers included
    values: dict[str, str | None]   # $Project::, $Package:: and User:: values for expressions
    _by_key: dict[str, Connection] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, data: bytes, project: ProjectInfo | None = None) -> "Package":
        return cls.from_root(ET.fromstring(data), project)

    @classmethod
    def from_root(cls, root: ET.Element, project: ProjectInfo | None = None) -> "Package":
        values = _values(root, project)
        conns, by_key = _connections(root, project, values)
        made: dict[int, Task] = {}
        p = cls(root.get(f"{_D}ObjectName") or "", root, project, conns, [], [], values, by_key)
        p.tasks = p._children(root, made)
        for e in root.iter(f"{_D}Executable"):
            if e is not root:
                p.every_task.append(made.get(id(e)) or p._task(e, made))
        return p

    def connection(self, ref: str | None) -> Connection | None:
        """A connection manager by DTSID, refId, Package/Project.ConnectionManagers[name] or name."""
        if not ref:
            return None
        ref = ref.removesuffix(":external")
        return self._by_key.get(ref) or self._by_key.get(re.sub(r".*\[(.*)\]$", r"\1", ref))

    def walk(self) -> list[Task]:
        """The package's tasks and their containers' tasks, in run order (event handlers left out)."""
        def go(tasks: list[Task]) -> list[Task]:
            return [x for t in tasks for x in (t, *go(t.tasks))]
        return go(self.tasks)

    # -------------------------------------------------------------------------------- reading --

    def _children(self, container: ET.Element, made: dict[int, Task]) -> list[Task]:
        execs = container.find(f"{_D}Executables")
        if execs is None:
            return []
        tasks = [self._task(e, made) for e in execs.findall(f"{_D}Executable")]
        after: dict[str, list[dict]] = {}
        pcs = container.find(f"{_D}PrecedenceConstraints")
        for pc in (pcs.findall(f"{_D}PrecedenceConstraint") if pcs is not None else []):
            expr = pc.get(f"{_D}Expression")
            after.setdefault(pc.get(f"{_D}To", ""), []).append(
                {"after": pc.get(f"{_D}From", "").rsplit("\\", 1)[-1],
                 "on": _ON.get(pc.get(f"{_D}Value", "0"), "success"), **({"expression": expr} if expr else {})})
        for t in tasks:
            t.runs_after = after.get(t.path, [])
        return _in_run_order(tasks, after)

    def _task(self, e: ET.Element, made: dict[int, Task]) -> Task:
        etype = e.get(f"{_D}ExecutableType", "")
        t = Task(e.get(f"{_D}ObjectName") or "", e.get(f"{_D}refId") or "", etype, KINDS.get(etype, "other"), e,
                 disabled=e.get(f"{_D}Disabled") == "True",
                 expressions={x.get(f"{_D}Name") or "": x.text or "" for x in e.findall(f"{_D}PropertyExpression")})
        made[id(e)] = t
        data = e.find(f"{_D}ObjectData")
        if data is not None:
            t.sql_data = data.find(f"{SQLTASK}SqlTaskData")
            if t.sql_data is not None:
                t.connection_ref = t.sql_data.get(f"{SQLTASK}Connection") or ""
                t.connection = self.connection(t.connection_ref)
            pipeline = data.find("pipeline")
            if pipeline is not None:
                t.components = [self._component(c) for c in pipeline.iter("component")]
        if t.kind in CONTAINERS:
            t.tasks = self._children(e, made)
        return t

    def _component(self, c: ET.Element) -> Component:
        ref = c.find("connections/connection")
        key = (ref.get("connectionManagerRefId") or ref.get("connectionManagerID") or "") if ref is not None else ""
        key = key.removesuffix(":external")
        return Component(c.get("name") or "", c.get("refId") or "", c.get("componentClassID") or "",
                         {p.get("name") or "": p.text or "" for p in c.findall("properties/property")},
                         key, self.connection(key), c)


def _in_run_order(tasks: list[Task], after: dict[str, list[dict]]) -> list[Task]:
    """Topological order by precedence constraints; document order breaks ties (the first ready task in
    document order goes next), and a cycle keeps the rest in document order. Linear in the constraints."""
    import heapq
    index = {t.name: i for i, t in enumerate(tasks)}
    waits = [0] * len(tasks)
    then: dict[int, list[int]] = {}
    for i, t in enumerate(tasks):
        for d in {a["after"] for a in after.get(t.path, []) if a["after"] in index}:
            waits[i] += 1
            then.setdefault(index[d], []).append(i)
    ready = [i for i, n in enumerate(waits) if n == 0]
    heapq.heapify(ready)
    done = [False] * len(tasks)
    ordered: list[Task] = []
    first = 0     # the first task in document order not done yet: next when everything left is in a cycle
    while len(ordered) < len(tasks):
        if ready:
            i = heapq.heappop(ready)
            if done[i]:
                continue
        else:
            while done[first]:
                first += 1
            i = first
        done[i] = True
        ordered.append(tasks[i])
        for j in then.get(i, ()):
            waits[j] -= 1
            if waits[j] == 0 and not done[j]:
                heapq.heappush(ready, j)
    return ordered


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


def _connections(root: ET.Element, project: ProjectInfo | None, vals: dict[str, str | None]
                 ) -> tuple[list[Connection], dict[str, Connection]]:
    """The package's connection managers, then the project's, each with the database it points at; and
    each by DTSID, refId, Package/Project.ConnectionManagers[name] and name (the package's first)."""
    managers = [(cm, "Package") for holder in root.iter(f"{_D}ConnectionManagers")
                for cm in holder.findall(f"{_D}ConnectionManager")]
    for data in (project.managers if project else []):
        try:
            managers.append((ET.fromstring(data), "Project"))
        except ET.ParseError:
            continue
    design = project is not None and project.design
    where = f" in {project.name}" if project and project.name else ""
    out: list[Connection] = []
    by_key: dict[str, Connection] = {}
    for cm, scope in managers:
        name = cm.get(f"{_D}ObjectName") or ""
        inner = cm.find(f"{_D}ObjectData/{_D}ConnectionManager")
        c = Connection(name, cm.get(f"{_D}CreationName") or "", scope, cm.get(f"{_D}DTSID") or "",
                       cm.get(f"{_D}refId") or f"{scope}.ConnectionManagers[{name}]",
                       (inner.get(f"{_D}ConnectionString") if inner is not None else None) or "",
                       {e.get(f"{_D}Name") or "": e.text or "" for e in cm.findall(f"{_D}PropertyExpression")},
                       inner is not None and (inner.get(f"{_D}Retain") or "").lower() == "true")
        decided, source = False, ""
        if scope == "Project":     # the catalog can set a project connection manager's properties
            for prop in ("InitialCatalog", "ConnectionString"):
                key = f"$project::cm.{name.lower()}.{prop.lower()}"
                if key in vals:
                    v = vals[key]
                    c.database, decided = (v.lower() if prop == "InitialCatalog" and v else database_of(v)), True
                    break
        if not decided:
            for prop in ("InitialCatalog", "ConnectionString"):
                if prop in c.expressions:
                    v = evaluate(c.expressions[prop], vals)
                    c.database, decided = (v.lower() if prop == "InitialCatalog" and v else database_of(v)), True
                    params = sorted(set(_PROJECT_PARAM.findall(c.expressions[prop])), key=str.lower)
                    if params:
                        source = " and ".join(f"$Project::{x}" for x in params) + where
                    break
        if not decided:
            c.database = database_of(c.connection_string)
        if scope == "Project" and not source:
            source = f"project connection manager {name}{where}"
        if design and source and c.database:
            c.assumed = f"{source} = {c.database.upper()}"
        out.append(c)
        for key in (c.ref_id, c.dtsid, f"{scope}.ConnectionManagers[{name}]", name):
            if key:
                by_key.setdefault(key, c)
    return out, by_key


# --------------------------------------------------------------------------- Execute Process tasks --

def process(p: ET.Element) -> dict:
    """What an Execute Process task starts: the executable, its arguments, and the script it runs."""
    exe = p.get("Executable", "")
    args = p.get("Arguments", "")
    out: dict = {"executable": exe, "arguments": args, "working_directory": p.get("WorkingDirectory")}
    base = ntpath.basename(exe).lower()
    try:
        words = shlex.split(args, posix=False)
    except ValueError:
        words = args.split()
    if base in ("powershell.exe", "pwsh.exe", "powershell", "pwsh"):
        out["runs"] = "powershell"
        script = next((words[i + 1] for i, w in enumerate(words[:-1]) if w.lower() in ("-file", "-f")), None)
        if script:
            out["script"] = script.strip('"')
    elif re.match(r"(python[\d.]*|py)(\.exe)?$", base):
        out["runs"] = "python"
        script = next((w for w in words if w.strip('"').lower().endswith(".py")), None)
        if script:
            out["script"] = script.strip('"')
    elif base in ("cmd.exe", "cmd"):
        out["runs"] = "cmd"
    else:
        out["runs"] = base or "process"
    for attr, key in (("StandardErrorVariable", "stderr_variable"), ("StandardOutputVariable", "stdout_variable")):
        if p.get(attr):
            out[key] = p.get(attr)
    if p.get("FailTaskIfReturnCodeIsNotSuccessValue") == "False":
        out["ignores_exit_code"] = True
    return out
