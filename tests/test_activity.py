"""ssisbliss activity, strays and remove-packages --strays on the climb's worlds: fake servers plus a real
git repository with a team remote that colleagues push branches to."""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CLIMB = Path(__file__).resolve().parents[1] / "climb"
if not CLIMB.is_dir():   # the source archive ships the package's tests, not the climb
    pytest.skip("climb/ is only in the repository", allow_module_level=True)
sys.path.insert(0, str(CLIMB))
from team import RELEASED, sql_world, strays_world  # noqa: E402
from world import COLLEAGUE, ME, task_package  # noqa: E402

from ssisbliss.ssis.deps import facts, related, sql_tables  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEV = ["--env", "dev", "--folder", "STAGE", "--project", "STAGE"]


def cli(w, *args):
    env = {"PATH": f"{REPO / '.venv' / 'bin'}:/usr/bin:/bin", "SSISBLISS_FAKE_USER": ME, "HOME": str(w.root),
           "SSISBLISS_FAKE_ROOT": str(w.root / "servers"), "SSISBLISS_STATE_DIR": str(w.state_dir)}
    p = subprocess.run([str(REPO / ".venv" / "bin" / "ssisbliss"), *args], cwd=w.work, env=env,
                       capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout)


def test_sql_tables_reads_and_writes():
    assert sql_tables("INSERT dbo.LoadLog (Package) SELECT N'x' FROM dbo.Product")[:2] == ({"dbo.product"}, {"dbo.loadlog"})
    assert sql_tables("UPDATE c SET x = ? FROM dbo.Customer c JOIN dbo.Region r ON r.Id = c.RegionId")[:2] == \
        ({"dbo.region"}, {"dbo.customer"})
    assert sql_tables("SELECT * INTO #t FROM Sales.Orders")[:2] == ({"sales.orders"}, set())
    assert sql_tables("TRUNCATE TABLE stage.Customer")[:2] == (set(), {"stage.customer"})
    assert sql_tables("EXEC dbo.LoadCdw @Mode = ?")[:2] == (set(), set())


def test_related_follows_calls_and_tables():
    all_facts = {n: facts(n, d) for n, d in RELEASED.items()}
    depends, used = related("Stage_Customer.dtsx", all_facts)
    assert [e.package for e in used] == ["Stage_SalesOrder.dtsx"] and "stage.customer" in used[0].via
    depends, used = related("Stage_Product.dtsx", all_facts)
    assert [e.package for e in used] == ["Main.dtsx"] and "Execute Package" in used[0].via


def test_activity_names_a_colleague_on_the_same_package_and_whose_version_dev_has(tmp_path):
    w = sql_world(tmp_path / "w")
    theirs = task_package("Stage_Customer.dtsx", revision=7, sql=["INSERT stage.Customer SELECT * FROM bronze.Customer"])
    w.colleague_branch("pr/42-customer-email", "Bob", {"Stage_Customer.dtsx": theirs})
    w.deploy_as("dev", COLLEAGUE, "Stage_Customer.dtsx", theirs)
    code, out = cli(w, "activity", *DEV, "--dir", "STAGE", "Stage_Customer")
    r = out["packages"][0]
    assert code == 0 and r["branches"][0]["branch"] == "origin/pr/42-customer-email"
    assert r["branches"][0]["author"].startswith("Bob") and not r["branches"][0].get("yours")
    assert r["server"]["branch"] == "origin/pr/42-customer-email"
    assert "Bob" in r["summary"] and "origin/pr/42-customer-email" in r["summary"]


def test_activity_finds_a_colleague_on_a_package_that_depends_on_mine(tmp_path):
    w = sql_world(tmp_path / "w")
    changed = task_package("Stage_SalesOrder.dtsx", revision=3, sql=[
        "INSERT stage.SalesOrder SELECT o.*, c.Name FROM bronze.SalesOrder o JOIN stage.Customer c ON c.Id = o.CustomerId"])
    w.colleague_branch("pr/57-orders", "Carol", {"Stage_SalesOrder.dtsx": changed})
    code, out = cli(w, "activity", *DEV, "--dir", "STAGE", "Stage_Customer.dtsx")
    used = out["packages"][0]["used_by"]
    assert used[0]["package"] == "Stage_SalesOrder.dtsx" and "stage.customer" in used[0]["via"]
    assert used[0]["others_working"][0]["branch"] == "origin/pr/57-orders"
    assert "Carol" in out["packages"][0]["summary"]


def test_activity_finds_a_dependency_that_only_exists_on_a_branch(tmp_path):
    w = sql_world(tmp_path / "w")
    w.colleague_branch("pr/60-main", "Carol", {"Main.dtsx": task_package("Main.dtsx", 2, calls=[
        "Stage_Product.dtsx", "Stage_Customer.dtsx"])})
    code, out = cli(w, "activity", *DEV, "--dir", "STAGE", "Stage_Customer.dtsx")
    used = out["packages"][0]["used_by"]
    assert any(u["package"] == "Main.dtsx" and "on origin/pr/60-main" in u["via"] for u in used)


def test_activity_lists_runs_by_people_not_by_sql_agent(tmp_path):
    w = sql_world(tmp_path / "w")
    dev = w.server("dev")
    now = datetime.now(timezone.utc)
    for i, (who, mins) in enumerate([("LAB\\bob", 20), ("NT SERVICE\\SQLAgent$DEV", 5), ("LAB\\svc_agent", 3)]):
        dev.execution_rows.append({"execution_id": 900 + i, "folder_name": "STAGE", "project_name": "STAGE",
                                   "package_name": "Stage_Customer.dtsx", "status": 7, "caller_name": who,
                                   "start_time": (now - timedelta(minutes=mins)).isoformat()})
    dev.save()
    (w.server_dir("dev") / "msdb.json").write_text(json.dumps(
        {"server_name": "LABSQL\\DEV", "jobs": [], "steps": [], "history": [], "run_as": ["LAB\\svc_agent"]}))
    code, out = cli(w, "activity", *DEV, "--dir", "STAGE", "Stage_Customer.dtsx")
    runs = out["packages"][0]["recent_runs_by_people"]
    assert [r["by"] for r in runs] == ["LAB\\bob"] and runs[0]["status"] == "succeeded"


def test_without_names_activity_lists_only_packages_someone_else_touches(tmp_path):
    w = sql_world(tmp_path / "w")
    w.colleague_branch("pr/42", "Bob", {"Stage_Product.dtsx": task_package("Stage_Product.dtsx", 5)})
    code, out = cli(w, "activity", *DEV, "--dir", "STAGE")
    names = [r["package"] for r in out["packages"]]
    assert "Stage_Product.dtsx" in names and "Main.dtsx" in names      # Main runs Stage_Product
    assert "Stage_Customer.dtsx" not in names and out["quiet"] >= 1


def test_deploy_plan_warns_but_does_not_block_when_others_work_on_it(tmp_path):
    w = sql_world(tmp_path / "w")
    w.colleague_branch("pr/57-orders", "Carol", {"Stage_SalesOrder.dtsx": task_package("Stage_SalesOrder.dtsx", 4)})
    w.edit_local("Stage_Customer.dtsx", task_package("Stage_Customer.dtsx", 2, sql=["INSERT stage.Customer SELECT 1"]))
    code, plan = cli(w, "deploy-packages", *DEV, "STAGE/Stage_Customer.dtsx")
    warn = next(c for c in plan["checks"] if c["check"] == "others_working:Stage_Customer.dtsx")
    assert code == 0 and plan["can_apply"] and warn["status"] == "warn" and "Carol" in warn["detail"]


def test_no_warning_when_nobody_else_is_working(tmp_path):
    w = sql_world(tmp_path / "w")
    w.edit_local("Stage_Customer.dtsx", task_package("Stage_Customer.dtsx", 2, sql=["INSERT stage.Customer SELECT 1"]))
    code, plan = cli(w, "deploy-packages", *DEV, "STAGE/Stage_Customer.dtsx")
    assert code == 0 and not any(c["check"].startswith("others_working") for c in plan["checks"])


# --------------------------------------------------------------------- strays --

def test_strays_with_evidence(tmp_path):
    w = strays_world(tmp_path / "w")
    code, out = cli(w, "strays", *DEV, "--dir", "STAGE")
    by = {r["package"]: r for r in out["strays"]}
    assert set(by) == {"Stage_Old.dtsx", "Stage_Try.dtsx", "Stage_New.dtsx", "Stage_Job.dtsx"}
    assert by["Stage_Old.dtsx"]["verdict"] == "stray" and "replaced" in by["Stage_Old.dtsx"]["removed_from_default_branch"]["subject"]
    assert by["Stage_Try.dtsx"]["verdict"] == "stray"
    assert by["Stage_New.dtsx"]["verdict"] == "keep for now" and "pr/70-new" in by["Stage_New.dtsx"]["why"]
    assert by["Stage_Job.dtsx"]["verdict"] == "keep for now" and "9 NIGHTLY" in by["Stage_Job.dtsx"]["why"]
    assert out["remove_command"].endswith("--strays Stage_Old.dtsx Stage_Try.dtsx")


def test_activity_calls_a_package_no_branch_has_a_stray_candidate_not_someones_work(tmp_path):
    w = strays_world(tmp_path / "w")
    code, out = cli(w, "activity", *DEV, "--dir", "STAGE", "Stage_Old", "Stage_Try", "Stage_New")
    by = {r["package"]: r for r in out["packages"]}
    for n in ("Stage_Old.dtsx", "Stage_Try.dtsx"):
        assert by[n]["server"]["state"].startswith("not in the default branch and on no branch"), by[n]
        assert "ssisbliss strays" in by[n]["server"]["state"] and "ssisbliss strays" in by[n]["summary"]
        assert "unpushed or local work" not in json.dumps(by[n])
    assert "replaced" in by["Stage_Old.dtsx"]["server"]["removed_from_default_branch"]["subject"]
    assert "pr/70-new" in by["Stage_New.dtsx"]["summary"]                  # on Bob's branch: not a stray candidate
    assert not by["Stage_New.dtsx"]["server"]["state"].startswith("not in the default branch")


def test_activity_still_calls_another_version_of_a_released_package_someones_work(tmp_path):
    w = sql_world(tmp_path / "w")
    w.deploy_as("dev", COLLEAGUE, "Stage_Customer.dtsx", task_package("Stage_Customer.dtsx", revision=9))
    code, out = cli(w, "activity", *DEV, "--dir", "STAGE", "Stage_Customer")
    assert "unpushed or local work" in out["packages"][0]["server"]["state"]


def test_remove_strays_removes_only_strays(tmp_path):
    w = strays_world(tmp_path / "w")
    code, plan = cli(w, "remove-packages", *DEV, "--dir", "STAGE", "--strays")
    assert code == 0, plan
    assert sorted(c["package"] for c in plan["changes"]) == ["Stage_Old.dtsx", "Stage_Try.dtsx"]
    code, res = cli(w, "remove-packages", *DEV, "--dir", "STAGE", "--strays", "--apply", "--plan-id", plan["plan_id"],
                    "--backup-dir", str(tmp_path / "bk"))
    assert res["status"] == "verified"
    left = set(w.hashes("dev"))
    assert {"Stage_New.dtsx", "Stage_Job.dtsx", "Stage_Customer.dtsx"} <= left and "Stage_Try.dtsx" not in left


def test_remove_strays_refuses_a_package_someone_works_on(tmp_path):
    w = strays_world(tmp_path / "w")
    code, plan = cli(w, "remove-packages", *DEV, "--dir", "STAGE", "--strays", "Stage_New")
    assert code == 2 and "pr/70-new" in plan["refused_because"][0]
    code, plan = cli(w, "remove-packages", *DEV, "--dir", "STAGE", "--strays", "Stage_Customer")
    assert code == 2 and "origin/main" in plan["refused_because"][0]


def test_strays_refuses_a_folder_that_is_not_the_project(tmp_path):
    w = strays_world(tmp_path / "w")
    code, out = cli(w, "strays", *DEV, "--dir", ".")
    assert code == 2 and "--dir STAGE" in out["error"]
    code, out = cli(w, "remove-packages", *DEV, "--dir", ".", "--strays")
    assert code == 2 and "--dir STAGE" in out["error"]
