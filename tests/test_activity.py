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


def test_sql_tables_work_out_each_text_once(monkeypatch):
    import sqlglot
    sql = "INSERT stage.OnlyHere SELECT * FROM bronze.OnlyHere"
    sql_tables.cache_clear()
    first = sql_tables(sql)
    monkeypatch.setattr(sqlglot, "parse", lambda *a, **k: pytest.fail("parsed the same text twice"))
    assert sql_tables(sql) == first == ({"bronze.onlyhere"}, {"stage.onlyhere"}, True)


def test_activity_does_not_read_a_server_package_your_copy_replaces(monkeypatch):
    from ssisbliss.ssis import activity as mod
    from ssisbliss.ssis.fake import ispac
    from ssisbliss.ssis.ispac import Ispac
    from ssisbliss.ssis.dtsx import package_sha256
    server = Ispac(ispac("P", {"Stage_Customer.dtsx": RELEASED["Stage_Customer.dtsx"], "Main.dtsx": RELEASED["Main.dtsx"]}))
    read = []
    real = mod.deps.facts
    monkeypatch.setattr(mod.deps, "facts", lambda n, d: read.append((n, d)) or real(n, d))
    theirs = task_package("Stage_Customer.dtsx", sql=["INSERT stage.Mine SELECT * FROM bronze.Mine"])
    found = mod.Activity(None, server=server, local={"stage_customer.dtsx": theirs}).facts()
    # your copy counts under the deployed spelling; only the package you do not have comes from the server
    assert read == [("Main.dtsx", RELEASED["Main.dtsx"]), ("Stage_Customer.dtsx", theirs)]
    assert list(found) == ["Main.dtsx", "Stage_Customer.dtsx"]
    assert found["Stage_Customer.dtsx"].writes == {"stage.mine"}
    assert found["Stage_Customer.dtsx"].package == "Stage_Customer.dtsx"
    assert server.package_hashes()["Main.dtsx"] == package_sha256(RELEASED["Main.dtsx"])


def test_package_hashes_are_worked_out_once_and_a_caller_cannot_change_them(monkeypatch):
    from ssisbliss.ssis import ispac as mod
    from ssisbliss.ssis.fake import ispac
    server = mod.Ispac(ispac("P", {"Main.dtsx": RELEASED["Main.dtsx"]}))
    first = server.package_hashes()
    first["Main.dtsx"] = "changed by a caller"
    monkeypatch.setattr(mod, "package_sha256", lambda d: pytest.fail("hashed the same project twice"))
    assert server.package_hashes()["Main.dtsx"] != "changed by a caller"


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


def _branches(s):
    return [(b.name, b.sha, b.yours, {k: vars(c) for k, c in b.changes.items()}) for b in s.branches]


def test_scan_reads_linear_branches_with_one_log_and_reports_what_the_per_branch_way_does(tmp_path, monkeypatch):
    from ssisbliss.ssis import gitscan
    w = sql_world(tmp_path / "w")
    pkg = lambda n, r: task_package(n, revision=r, sql=[f"SELECT {r}"])   # noqa: E731
    w.colleague_branch("pr/plain", "Bob", {"Stage_Customer.dtsx": pkg("Stage_Customer.dtsx", 7)})
    clone = w.root / "clone"
    subprocess.run(["git", "clone", "-q", str(w.root / "origin.git"), str(clone)], check=True)
    g = lambda *a: subprocess.run(["git", *a], cwd=clone, check=True, capture_output=True)   # noqa: E731
    g("config", "user.email", "dev@example.com")
    g("config", "user.name", "Dev")

    def commit(message, **files):
        for name, data in files.items():
            p = clone / "STAGE" / name.replace("__", ".")
            p.write_bytes(data) if data is not None else p.unlink()
        g("add", "-A")
        g("commit", "-q", "-m", message)

    # several commits; a package added and removed again, one changed and put back, one deleted, one only outside
    g("checkout", "-q", "-b", "pr/mixed", "origin/main")
    original = (clone / "STAGE" / "Stage_Product.dtsx").read_bytes()
    commit("a", Stage_Product__dtsx=pkg("Stage_Product.dtsx", 2), Temp__dtsx=pkg("Temp.dtsx", 1),
           Stage_SalesOrder__dtsx=pkg("Stage_SalesOrder.dtsx", 2))
    commit("b", Stage_Product__dtsx=original, Temp__dtsx=None, Stage_SalesOrder__dtsx=pkg("Stage_SalesOrder.dtsx", 3))
    commit("c", Main__dtsx=None)
    (clone / "STAGE" / "sub").mkdir()
    (clone / "STAGE" / "sub" / "Deep.dtsx").write_bytes(pkg("Deep.dtsx", 1))
    (clone / "STAGE" / "notes.txt").write_text("x")
    commit("d")
    g("push", "-q", "origin", "pr/mixed")
    # a merge inside the branch: answered the per-branch way
    g("checkout", "-q", "-b", "pr/side", "origin/main")
    commit("side", Stage_Customer__dtsx=pkg("Stage_Customer.dtsx", 8))
    g("checkout", "-q", "-b", "pr/merged", "origin/main")
    commit("own", Stage_Product__dtsx=pkg("Stage_Product.dtsx", 9))
    g("merge", "-q", "--no-edit", "pr/side")
    g("push", "-q", "origin", "pr/merged")
    # a branch already merged into main changes nothing
    g("checkout", "-q", "-B", "pr/old", "origin/main")
    g("push", "-q", "origin", "pr/old")
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=w.work, check=True)

    calls = []
    real = gitscan._git_bytes
    monkeypatch.setattr(gitscan, "_git_bytes", lambda cwd, *a: calls.append(a[0]) or real(cwd, *a))
    fast = gitscan.scan(w.work / "STAGE")
    assert calls.count("merge-base") == 1 and calls.count("diff") == 1      # only the branch with a merge
    assert calls.count("log") == 3             # the one for all branches, plus that branch's two packages
    assert _branches(fast) == _branches(_slow_scan(monkeypatch, gitscan, w))
    mixed = next(b for b in fast.branches if b.name == "origin/pr/mixed")
    assert sorted(mixed.changes) == ["main.dtsx", "stage_salesorder.dtsx"]
    assert mixed.changes["main.dtsx"].deleted and mixed.changes["main.dtsx"].path == ""
    assert mixed.changes["stage_salesorder.dtsx"].commits == 2
    assert "origin/pr/old" not in [b.name for b in fast.branches]


def _slow_scan(monkeypatch, gitscan, w):
    with monkeypatch.context() as m:
        m.setattr(gitscan, "_history", lambda *a: None)
        return gitscan.scan(w.work / "STAGE")


def _old_facts(gitscan, directory):
    """The repository facts as separate git calls would give them, to hold the shared reads against."""
    git = lambda *a: gitscan._git(directory, *a)   # noqa: E731
    top = git("rev-parse", "--show-toplevel")
    default = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    for ref in ("origin/main", "origin/master", "main", "master"):
        default = default or (ref if git("rev-parse", "--verify", "--quiet", ref) else None)
    return (Path(top), (git("rev-parse", "--show-prefix") or "").rstrip("/"), default,
            git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"))


@pytest.mark.parametrize("state", ["origin_head", "no_origin_head", "other_branch", "detached", "worktree", "top"])
def test_scan_reads_repository_facts_in_one_go_and_finds_what_separate_calls_do(tmp_path, monkeypatch, state):
    from ssisbliss.ssis import gitscan
    w = sql_world(tmp_path / "w")
    w.colleague_branch("pr/plain", "Bob", {"Stage_Customer.dtsx": task_package("Stage_Customer.dtsx", revision=7)})
    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(w.root / "origin.git"), str(repo)], check=True, capture_output=True)
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)   # noqa: E731
    directory = repo / "STAGE"
    if state == "no_origin_head":
        g("remote", "set-head", "origin", "--delete")
    elif state == "other_branch":
        g("checkout", "-q", "-b", "mine", "--track", "origin/pr/plain")
    elif state == "detached":
        g("checkout", "-q", "--detach")
    elif state == "worktree":
        g("worktree", "add", "-q", "-b", "wt", str(tmp_path / "wt"), "origin/pr/plain")
        g("branch", "--set-upstream-to=origin/pr/plain", "wt")
        directory = tmp_path / "wt" / "STAGE"
    elif state == "top":
        directory = repo
    root, prefix, default, upstream = _old_facts(gitscan, directory)

    calls = []
    real = gitscan._git_bytes
    monkeypatch.setattr(gitscan, "_git_bytes", lambda cwd, *a: calls.append(a) or real(cwd, *a))
    s = gitscan.scan(directory)
    assert (s.root, s.prefix, s.default) == (root, prefix, default)
    assert [c[0] for c in calls].count("rev-parse") == 1 and "symbolic-ref" not in [c[0] for c in calls]
    assert sorted(c[0] for c in calls) == ["config", "for-each-ref", "log", "ls-tree", "rev-parse"]
    if state == "top":                     # the packages are in STAGE/, so at the top no branch changes one
        assert s.branches == []
    else:
        assert [b.yours for b in s.branches if b.name == "origin/pr/plain"] == [upstream == "origin/pr/plain"]


def test_scan_without_a_branch_called_main_still_finds_nothing_or_a_tag(tmp_path):
    from ssisbliss.ssis import gitscan
    g = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)   # noqa: E731
    g("init", "-q", "-b", "trunk")
    assert gitscan.scan(tmp_path).default is None and "no default branch" in gitscan.scan(tmp_path).note
    g("-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-q", "--allow-empty", "-m", "one")
    g("tag", "main")
    assert gitscan.scan(tmp_path).default == "main"


def test_blobs_reads_files_at_revisions_through_one_git_process(tmp_path, monkeypatch):
    from ssisbliss.ssis import gitscan
    from ssisbliss.ssis.dtsx import package_sha256
    g = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    g("init", "-q", "-b", "main")
    g("config", "user.email", "me@example.com")
    g("config", "user.name", "Me")
    (tmp_path / "sub").mkdir()
    data = task_package("Load One.dtsx", sql=["SELECT 1"])
    (tmp_path / "sub" / "Load One.dtsx").write_bytes(data)
    (tmp_path / "sub" / "Copy.dtsx").write_bytes(data)
    (tmp_path / "sub" / "Broken.dtsx").write_bytes(b"not xml")
    g("add", "-A")
    g("commit", "-q", "-m", "one")
    started = []
    real = subprocess.Popen
    monkeypatch.setattr(gitscan.subprocess, "Popen", lambda *a, **k: started.append(a) or real(*a, **k))
    calls = []
    real_parse = gitscan.package_sha256
    monkeypatch.setattr(gitscan, "package_sha256", lambda d: calls.append(1) or real_parse(d))
    b = gitscan.Blobs(tmp_path)
    assert b.show("HEAD", "sub/Load One.dtsx") == data
    assert b.show("HEAD", "sub/Missing.dtsx") is None
    assert b.show("HEAD", "sub") is None                       # a tree is not a file
    assert b.sha("HEAD", "sub/Load One.dtsx") == package_sha256(data)
    assert b.sha("HEAD", "sub/Copy.dtsx") == package_sha256(data)   # same blob: parsed once
    assert b.sha("HEAD", "sub/Broken.dtsx") == package_sha256(b"not xml")
    assert b.sha("HEAD", "sub/Missing.dtsx") is None
    assert b.show("HEAD", "sub/Load One.dtsx") == data           # still in step after all of that
    assert len(started) == 1 and len(calls) == 2
    b.close()
    assert b.show("HEAD", "sub/Copy.dtsx") == data and len(started) == 2
    b.close()
