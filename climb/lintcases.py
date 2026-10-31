"""Lint scenarios: checking packages before a pull request, laying out their SQL, and describing SQL
changes for a pull request. Registered in scenarios.py as suite "lint"."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ssisbliss.ssis.sqlformat import format_sql
from ssisbliss.ssis.sqlsites import replace, sites
from team import sql_world
from world import World, git, task_package


def _snapshot_files(w: World) -> None:
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(w.project_dir.glob("*.dtsx"))}
    (w.root / "local.json").write_text(json.dumps(files, indent=1))


def changed_files(w: World) -> set[str]:
    before = json.loads((w.root / "local.json").read_text())
    now = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in w.project_dir.glob("*.dtsx")}
    return {n for n in set(before) | set(now) if before.get(n) != now.get(n)}


def with_bindings(data: bytes, task: str, n: int) -> bytes:
    """Give an Execute SQL task n parameter mappings (as Visual Studio's Parameter Mapping page writes them)."""
    marker = f'DTS:ObjectName="{task}">'.encode()
    i = data.index(marker)
    j = data.index(b"/>", data.index(b"SQLTask:SqlTaskData", i))
    binds = "".join(f'<SQLTask:ParameterBinding SQLTask:ParameterName="{k}" SQLTask:DtsVariableName="User::v{k}" />'
                    for k in range(n)).encode()
    return data[:j] + b">" + binds + b"</SQLTask:SqlTaskData>" + data[j + 2:]


def before_pr(root: Path) -> World:
    """Stage_Customer on the developer's branch: a DELETE without WHERE, and two ? markers with one mapping."""
    w = sql_world(root)
    git(w.work, "checkout", "-q", "-b", "pr/customer-cleanup")
    data = task_package("Stage_Customer.dtsx", 3, sql=[
        "DELETE FROM stage.Customer",
        "INSERT INTO stage.Customer (Id, Name) SELECT Id, Name FROM bronze.Customer WHERE Region = ? AND Created > ?"])
    w.edit_local("Stage_Customer.dtsx", with_bindings(data, "SQL 2", 1))
    git(w.work, "commit", "-q", "-am", "Stage_Customer: reload")
    w.snapshot()
    _snapshot_files(w)
    return w


def before_pr_ok(w: World, text: str) -> bool:
    from scenarios import mentions
    return (not changed_files(w) and mentions(text, "WHERE", "every row", "all rows", "full-table", "full table", "whole table") and mentions(text, "parameter mapping", "mapping", "binding", "? marker", "markers")
            and mentions(text, "2", "two"))


def make_readable(root: Path) -> World:
    w = sql_world(root)
    git(w.work, "checkout", "-q", "-b", "pr/customer-columns")
    w.edit_local("Stage_Customer.dtsx", task_package("Stage_Customer.dtsx", 2, sql=[
        "truncate table stage.Customer",
        "insert into stage.Customer (Id, Name, Segment) select c.Id, c.Name, s.Segment from bronze.Customer c "
        "left join bronze.Segment s on s.CustomerId = c.Id where c.Active = 1 and c.Deleted = 0"]))
    w.snapshot()
    _snapshot_files(w)
    return w


def make_readable_ok(w: World) -> bool:
    data = (w.project_dir / "Stage_Customer.dtsx").read_bytes()
    return changed_files(w) == {"Stage_Customer.dtsx"} and all(format_sql(s.sql)[0] == s.sql for s in sites(data))


def pr_description(root: Path) -> World:
    """On the branch, the Stage_SalesOrder load gets a new condition; the .dtsx stores the SQL on one line."""
    w = sql_world(root)
    git(w.work, "checkout", "-q", "-b", "pr/orders-north")
    data = (w.project_dir / "Stage_SalesOrder.dtsx").read_bytes()
    found = sites(data)
    new = found[0].sql + " AND o.Region = 'North'"
    w.edit_local("Stage_SalesOrder.dtsx", replace(data, {0: new}, found))
    git(w.work, "commit", "-q", "-am", "Stage_SalesOrder: only the north")
    w.snapshot()
    _snapshot_files(w)
    return w


def pr_description_ok(w: World, text: str) -> bool:
    from scenarios import mentions
    return not changed_files(w) and mentions(text, "o.Region = 'North'") and mentions(text, "Stage_SalesOrder")


def _slow_flow() -> bytes:
    """The data flow of Stage_Customer as a first version often is: a lookup without a cache, an OLE DB Command per
    row, and a destination without fast load (the package also keeps its SQL tasks)."""
    def comp(cls, name, **props):
        p = "".join(f'<property name="{k}">{v}</property>' for k, v in props.items())
        return f'<component refId="Package\\Load customers\\{name}" componentClassID="{cls}" name="{name}"><properties>{p}</properties></component>'
    flow = (comp("Microsoft.OLEDBSource", "Customers", AccessMode="2",
                 SqlCommand="SELECT Id, Name, SegmentCode FROM bronze.Customer")
            + comp("Microsoft.Lookup", "Find segment", CacheType="2",
                   SqlCommand="SELECT SegmentCode, SegmentId FROM dbo.Segment")
            + comp("Microsoft.OLEDBCommand", "Mark loaded",
                   SqlCommand="UPDATE bronze.Customer SET Loaded = 1 WHERE Id = ?")
            + comp("Microsoft.OLEDBDestination", "stage Customer", AccessMode="0", OpenRowset="[stage].[Customer]"))
    data = task_package("Stage_Customer.dtsx", 3, sql=["TRUNCATE TABLE stage.Customer;"])
    task = ('<DTS:Executable DTS:refId="Package\\Load customers" DTS:ExecutableType="Microsoft.Pipeline" '
            f'DTS:ObjectName="Load customers"><DTS:ObjectData><pipeline version="1"><components>{flow}</components>'
            '</pipeline></DTS:ObjectData></DTS:Executable>').encode()
    return data.replace(b"</DTS:Executables>", task + b"</DTS:Executables>")


def slow_package(root: Path) -> World:
    w = sql_world(root)
    git(w.work, "checkout", "-q", "-b", "pr/customer-segments")
    w.edit_local("Stage_Customer.dtsx", _slow_flow())
    git(w.work, "commit", "-q", "-am", "Stage_Customer: segments")
    w.snapshot()
    _snapshot_files(w)
    return w


def slow_package_ok(w: World, text: str) -> bool:
    from scenarios import mentions
    return (not changed_files(w) and mentions(text, "OLE DB Command", "Mark loaded")
            and mentions(text, "fast load", "fast-load", "fastload") and mentions(text, "cache"))
