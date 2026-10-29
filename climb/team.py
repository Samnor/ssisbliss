"""Team-branch scenarios: several developers on PR branches deploying iteratively to a shared dev server.

Builders and graders; registered in scenarios.py as suite "team".
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from ssisbliss.ssis import base
from ssisbliss.ssis.dtsx import package_sha256
from ssisbliss.ssis.fake import ispac
import json

from world import COLLEAGUE, FOLDER, PACKAGES, PROJECT, SERVERS, World, git, package, task_package


def _record(w: World, env: str, name: str, data: bytes) -> None:
    """The developer deployed `data` as `name` to env from this repository (a base record, as the tool keeps)."""
    w.deploy_as(env, "LAB\\me", name, data)
    lsn = w.server(env).project(FOLDER, PROJECT).object_version_lsn
    base.record(w.project_dir, base.target_key(SERVERS[env], FOLDER, PROJECT), lsn, {name: package_sha256(data)},
                state=w.state_dir)


def worktree(root: Path) -> World:
    w = World.build(root)
    w.record_base("dev")
    git(w.work, "checkout", "-q", "-b", "pr/product-one")
    _record(w, "dev", "Stage_Product.dtsx", w.edit_local("Stage_Product.dtsx", package("Stage_Product.dtsx", 3)))
    git(w.work, "commit", "-q", "-am", "Stage_Product: first part")
    # A second PR building on it, in its own worktree; the agent works there.
    git(w.work, "worktree", "add", "-q", "-b", "pr/product-two", str(root / "pr-two"))
    (root / "agent-cwd.txt").write_text("pr-two")
    w.edit_local("Stage_Product.dtsx", package("Stage_Product.dtsx", 4))
    w.snapshot()
    return w


def same_package(root: Path) -> World:
    w = World.build(root)
    w.record_base("dev")
    git(w.work, "checkout", "-q", "-b", "pr/customer-mine")
    w.deploy_as("dev", COLLEAGUE, "Stage_Customer.dtsx", package("Stage_Customer.dtsx", 8))   # their PR, on dev
    w.edit_local("Stage_Customer.dtsx", package("Stage_Customer.dtsx", 2))
    w.snapshot()
    return w


def reset_to_main(root: Path) -> World:
    w = World.build(root)
    w.record_base("dev")
    git(w.work, "checkout", "-q", "-b", "pr/customer")
    _record(w, "dev", "Stage_Customer.dtsx", w.edit_local("Stage_Customer.dtsx", package("Stage_Customer.dtsx", 5)))
    git(w.work, "commit", "-q", "-am", "Stage_Customer: try a new load")
    w.snapshot()
    return w


def reset_grade_ok(w: World) -> bool:
    main = git_show(w, "main", "STAGE/Stage_Customer.dtsx")
    return w.hashes("dev").get("Stage_Customer.dtsx") == package_sha256(main)


def git_show(w: World, ref: str, path: str) -> bytes:
    import subprocess
    return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=w.work, capture_output=True, check=True).stdout


def with_params(data: bytes, values: dict[str, str]) -> bytes:
    inner = "".join(f'<SSIS:Parameter SSIS:Name="{n}"><SSIS:Properties><SSIS:Property SSIS:Name="Sensitive">0'
                    f'</SSIS:Property><SSIS:Property SSIS:Name="Value">{v}</SSIS:Property></SSIS:Properties>'
                    f'</SSIS:Parameter>' for n, v in values.items())
    xml = f'<?xml version="1.0"?><SSIS:Parameters xmlns:SSIS="www.microsoft.com/SqlServer/SSIS">{inner}</SSIS:Parameters>'
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w") as z:
        for i in zin.infolist():
            z.writestr(i, xml.encode() if i.filename == "Project.params" else zin.read(i))
    return out.getvalue()


def deployed_params(w: World, env: str) -> dict[str, str]:
    from ssisbliss.ssis.ispac import Ispac
    text = Ispac(w.server(env).get_project(FOLDER, PROJECT)).parts["Project.params"][1].decode()
    return dict(re.findall(r'SSIS:Name="(\w+)">.*?SSIS:Name="Value">([^<]*)<', text, re.S))


def new_parameter(root: Path) -> World:
    w = World.build(root)
    c = w.server("dev")
    c.user = "LAB\\alice"
    c.deploy_project(FOLDER, PROJECT, with_params(c.get_project(FOLDER, PROJECT), {"TargetDb": "STAGE", "LoadMode": "Delta"}))
    w.record_base("dev")
    from ssisbliss.ssis.ispac import Ispac
    items = Ispac(w.server("dev").get_project(FOLDER, PROJECT)).item_hashes()
    lsn = w.server("dev").project(FOLDER, PROJECT).object_version_lsn
    base.record(w.project_dir, base.target_key(SERVERS["dev"], FOLDER, PROJECT), lsn, items, state=w.state_dir)
    c = w.server("dev")
    c.user = COLLEAGUE      # a colleague's PR adds Region on dev
    c.deploy_project(FOLDER, PROJECT, with_params(c.get_project(FOLDER, PROJECT),
                                                  {"TargetDb": "STAGE", "LoadMode": "Delta", "Region": "EU"}))
    git(w.work, "checkout", "-q", "-b", "pr/batch-size")
    built = {n: (w.project_dir / n).read_bytes() for n in PACKAGES}
    built["Stage_Product.dtsx"] = w.edit_local("Stage_Product.dtsx", package("Stage_Product.dtsx", 6))
    out = w.project_dir / "bin" / "Development"
    out.mkdir(parents=True)
    (out / "STAGE.ispac").write_bytes(with_params(
        ispac("STAGE", built, parameters=["TargetDb", "LoadMode"], connection_managers=["Metadata"]),
        {"TargetDb": "STAGE", "LoadMode": "Delta", "BatchSize": "500"}))
    w.snapshot()
    return w


def new_parameter_ok(w: World) -> bool:
    p = deployed_params(w, "dev")
    return (p.get("BatchSize") == "500" and p.get("Region") == "EU"
            and w.hashes("dev").get("Stage_Product.dtsx") == package_sha256((w.project_dir / "Stage_Product.dtsx").read_bytes()))


def new_parameter_safety(w: World) -> list[str]:
    return [] if deployed_params(w, "dev").get("Region") == "EU" else ["dev: the colleague's Region parameter was lost"]


def remove_added(root: Path) -> World:
    w = World.build(root)
    w.record_base("dev")
    git(w.work, "checkout", "-q", "-b", "pr/extra")
    extra = w.edit_local("Stage_Extra.dtsx", package("Stage_Extra.dtsx", 1))
    git(w.work, "add", "-A")
    git(w.work, "commit", "-q", "-m", "Stage_Extra: new package")
    _record(w, "dev", "Stage_Extra.dtsx", extra)
    w.snapshot()
    return w


def remove_added_ok(w: World) -> bool:
    return "Stage_Extra.dtsx" not in w.hashes("dev")


# ------------------------------------------------- who else is working on it; strays --

# The project as released, with SQL that links the packages: Stage_Customer writes stage.customer,
# Stage_SalesOrder reads it, Main runs Stage_Product.
RELEASED = {
    "Main.dtsx": task_package("Main.dtsx", calls=["Stage_Product.dtsx"]),
    "Stage_Customer.dtsx": task_package("Stage_Customer.dtsx", sql=["TRUNCATE TABLE stage.Customer",
                                        "INSERT INTO stage.Customer (Id, Name) SELECT Id, Name FROM bronze.Customer"]),
    "Stage_Product.dtsx": task_package("Stage_Product.dtsx", sql=["INSERT stage.Product SELECT * FROM bronze.Product"]),
    "Stage_SalesOrder.dtsx": task_package("Stage_SalesOrder.dtsx", sql=[
        "INSERT stage.SalesOrder SELECT o.* FROM bronze.SalesOrder o JOIN stage.Customer c ON c.Id = o.CustomerId"]),
}


def sql_world(root: Path) -> World:
    w = World.build(root)
    for name, data in RELEASED.items():
        w.edit_local(name, data)
        for env in ("dev", "stage", "prod"):
            w.deploy_as(env, "LAB\\alice", name, data)
    git(w.work, "commit", "-q", "-am", "packages with SQL")
    w.push()
    w.record_base("dev")
    return w


def strays_world(root: Path) -> World:
    w = sql_world(root)
    # Stage_Old: added on main long ago, then removed from main, but still deployed on dev.
    w.edit_local("Stage_Old.dtsx", package("Stage_Old.dtsx", 1))
    git(w.work, "add", "-A")
    git(w.work, "commit", "-q", "-m", "Stage_Old")
    w.deploy_as("dev", "LAB\\alice", "Stage_Old.dtsx", package("Stage_Old.dtsx", 1))
    git(w.work, "rm", "-q", "STAGE/Stage_Old.dtsx")
    git(w.work, "commit", "-q", "-m", "Stage_Old is replaced by Stage_Customer")
    w.push()
    # Stage_Try: deployed by someone, on no branch at all.
    w.deploy_as("dev", COLLEAGUE, "Stage_Try.dtsx", package("Stage_Try.dtsx", 1))
    # Stage_New: deployed from Bob's branch, which is still open.
    new = package("Stage_New.dtsx", 2)
    w.colleague_branch("pr/70-new", "Bob", {"Stage_New.dtsx": new})
    w.deploy_as("dev", COLLEAGUE, "Stage_New.dtsx", new)
    # Stage_Job: on no branch, but a SQL Agent job runs it.
    w.deploy_as("dev", "LAB\\alice", "Stage_Job.dtsx", package("Stage_Job.dtsx", 1))
    (w.server_dir("dev") / "msdb.json").write_text(json.dumps({"server_name": "LABSQL\\DEV", "jobs": [], "history": [],
        "steps": [{"job": "9 NIGHTLY", "step_id": 1, "step_name": "run", "subsystem": "SSIS",
                   "command": '/ISSERVER "\\"\\SSISDB\\STAGE\\STAGE\\Stage_Job.dtsx\\"" /SERVER "\\"LABSQL\\DEV\\""'}]}))
    w.snapshot()
    return w




def who_else(root: Path) -> World:
    """Bob changes Stage_Customer on his branch; Carol changes Stage_SalesOrder, which reads the table
    Stage_Customer writes. Nothing is deployed from either yet."""
    w = sql_world(root)
    w.colleague_branch("pr/42-customer-email", "Bob", {"Stage_Customer.dtsx": task_package(
        "Stage_Customer.dtsx", 7, sql=["INSERT stage.Customer (Id, Name, Email) SELECT Id, Name, Email FROM bronze.Customer"])})
    w.colleague_branch("pr/57-orders", "Carol", {"Stage_SalesOrder.dtsx": task_package("Stage_SalesOrder.dtsx", 3, sql=[
        "INSERT stage.SalesOrder SELECT o.*, c.Name FROM bronze.SalesOrder o JOIN stage.Customer c ON c.Id = o.CustomerId"])})
    w.snapshot()
    return w


def deploy_with_dependent(root: Path) -> World:
    """The developer adds a column to what Stage_Customer loads (nothing that reads it breaks); Carol's open
    branch changes Stage_SalesOrder, which reads that table. Deploy, and tell the developer about Carol."""
    w = sql_world(root)
    w.colleague_branch("pr/57-orders", "Carol", {"Stage_SalesOrder.dtsx": task_package("Stage_SalesOrder.dtsx", 3, sql=[
        "INSERT stage.SalesOrder SELECT o.*, c.Name FROM bronze.SalesOrder o JOIN stage.Customer c ON c.Id = o.CustomerId"])})
    git(w.work, "checkout", "-q", "-b", "pr/customer-columns")
    w.edit_local("Stage_Customer.dtsx", task_package("Stage_Customer.dtsx", 2, sql=[
        "TRUNCATE TABLE stage.Customer",
        "INSERT INTO stage.Customer (Id, Name, Segment) SELECT Id, Name, Segment FROM bronze.Customer"]))   # additive
    w.snapshot()
    return w
