"""lab.py verify: every real-server check in one run, each compared with what the lab established.

Run after merging a climb or changing a tool. Installs the current wheel first, needs the lab up
(lab.py up / start). Each check prints PASS or FAIL; the run exits non-zero on any FAIL. The full
output of each check is kept in lab/verify-runs/<timestamp>/.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable

LAB_DIR = Path(__file__).resolve().parent
TOOLS = LAB_DIR / "tools"


def first_json(text: str):
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[" and not text.startswith("[lab]", i):
            try:
                return dec.raw_decode(text[i:])[0]
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON in output")


def run(lab, iid: str, version: str, out: Path, only: list[str] | None = None) -> bool:
    ctx = lab.lab_context(version)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn: Callable[[], tuple[bool, str, str]]) -> None:
        if only and name not in only:
            return
        lab.log(f"verify: {name}")
        started = time.time()
        try:
            ok, detail, raw = fn()
        except SystemExit as e:   # run_powershell exits on a failed SSM command
            ok, detail, raw = False, f"command failed: {str(e)[:300]}", str(e)
        except Exception as e:  # noqa: BLE001
            ok, detail, raw = False, f"{type(e).__name__}: {e}"[:300], ""
        (out / f"{name}.txt").write_text(raw)
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name} ({time.time() - started:.0f}s): {detail}", flush=True)

    def ps(script: str, timeout: int = 1800) -> str:
        return lab.run_powershell(iid, (TOOLS / script).read_text(), ctx, timeout_s=timeout, label=script)

    def py(script: str, args: str = "", timeout: int = 1800) -> str:
        return lab.run_python(iid, TOOLS / script, ctx, args, timeout_s=timeout)

    lab.install_tools(iid)

    def smoke():
        raw = ps("smoke-ssis.ps1")
        return "SMOKE PASSED" in raw, "pull, package deploy, subset deploys, refusal, rollback", raw
    check("deploy-smoke", smoke)

    def devlogin():
        raw = ps("devlogin-ssis.ps1")
        ok = ("MODIFY permission on the project" in raw and '"status": "verified"' in raw and "restored SILVER" in raw
              and re.search(r"others check: .*labdev", raw) is not None)
        return ok, "deploy as a Windows user with folder permissions only; a rollback past it is refused", raw

    check("developer-login", devlogin)

    def agent():
        raw = ps("agent-chain.ps1")
        d = first_json(raw)
        ok = (d.get("broken_verdict") == "failed at 3 SILVER" and str(d.get("history_next", "")).startswith("ssisbliss executions --id")
              and d.get("failed_task_kind") == "sql" and d.get("failed_task_target_db") == "GOLD_ARCHIVE"
              and str(d.get("skipped_verdict", "")).startswith("stopped after 2 BRONZE") and "quit reporting success" in str(d.get("skipped_why")))
        return ok, f"broken: {d.get('broken_verdict')}; skipped: {d.get('skipped_verdict')}", raw
    check("agent-chain", agent)

    def items_remove():
        raw = py("items-remove.py")
        return "ITEMS-REMOVE PASSED" in raw, "per-item project parameter deploy keeps a colleague's; remove-packages", raw
    check("items-and-remove", items_remove)

    def rollback_others():
        raw = py("rollback-others.py")
        d = first_json(raw)
        return bool(d.get("ok")), f"login {d.get('login')}: your own deploys and already-undone ones do not block a rollback", raw
    check("rollback-others", rollback_others)

    def activity_server():
        raw = py("activity-server.py")
        d = first_json(raw)
        return (bool(d.get("ok")), f"run as {d.get('run_as')}; Main run by jobs {d.get('jobs_running_main')}; "
                f"{d.get('main_runs_all')} agent runs hidden; people: {[r['by'] for r in d.get('customer_runs_by_people', [])]}", raw)
    check("activity-server", activity_server)

    def format_roundtrip():
        raw = py("format-roundtrip.py")
        d = first_json(raw)
        return (bool(d.get("ok")), f"{d.get('laid_out')}/{d.get('statements')} statements laid out; SSIS reads them back: "
                f"{d.get('object_model_reads_back_formatted')}; runs {d.get('run_status')}; server parser accepts all "
                f"{d.get('seeded_statements')} seeded statements: {not d.get('server_rejected_seeded')}", raw)
    check("format-roundtrip", format_roundtrip)

    def microsoft_deploys():
        raw = py("compare_deploy.py")
        r = {x["method"]: x for x in first_json(raw)["results"]}
        ours = [r[m] for m in ("ssisbliss-project", "ssisbliss-packages")]
        theirs = [r[m] for m in ("wizard", "ssisdeploy")]
        ok = (all(x["developer_change_deployed"] and x["colleague_work_kept"] and x["bindings_kept"]
                  and x["environment_references_kept"] for x in ours)
              and all(x["developer_change_deployed"] and not x["colleague_work_kept"] for x in theirs))
        return ok, "ssisbliss keeps a colleague's package; ISDeploymentWizard/SSISDeploy project deploys still overwrite it", raw
    check("project-deploy-comparison", microsoft_deploys)

    def corpus():
        raw = ps("build-corpus.ps1", 3600)
        return raw.count(" done") >= 5, "Visual Studio (devenv) and SSISBuild builds of the test projects", raw
    check("build-corpus", corpus)

    def builds():
        raw = py("compare_build.py")
        res = first_json(raw)
        ok = all(x.get("vs_deploy") == x.get("ours_deploy") == x.get("ssisbuild_deploy") == "succeeded"
                 and x.get("packages_identical")
                 and x.get("ours_equals_ssisbuild", {}).get("parameters")
                 and (x["variant"] == "drift" or x.get("ours_equals_ssisbuild", {}).get("packages")) for x in res)
        return ok, "ssisbliss build deploys like Visual Studio's build (packages) and SSISBuild's (parameters)", raw
    check("build-comparison", builds)

    def package_deploys():
        raw = py("compare_package_deploy.py", timeout=3000)
        d = first_json(raw)
        deployed = [x for x in d["results"] if x["method"] != "wizard package mode, bin .ispac"]
        ok = (all(x["changed_on_server"] == ["Load_B.dtsx"] for x in deployed)
              and all(d["stored_result_same_as_first_method"].values()) and len(d["stored_result_same_as_first_method"]) == 4)
        return ok, "after a Visual Studio Rebuild, wizard package mode and ssisbliss leave the same project", raw
    check("package-deploy-comparison", package_deploys)

    def visual_studio_ui():
        lab.ensure_desktop_session(iid, ctx)
        prep = py("vs-ui/prepare.py")
        py("vs-ui/capture.py", "start")
        files = lab.run_in_desktop(iid, ctx, (TOOLS / "vs-ui" / "uia.ps1").read_text() + "\n"
                                   + (TOOLS / "vs-ui" / "deploy-package.ps1").read_text(), out / "vs-ui", timeout=1200)
        cap = first_json(py("vs-ui/capture.py", "stop"))
        ui = json.loads((out / "vs-ui" / "result.json").read_text("utf-8-sig")) if (out / "vs-ui" / "result.json").exists() else {}
        ok = (all(s["ok"] for s in ui.get("steps", [{"ok": False}])) and cap["procedures"].count("catalog.deploy_packages") == 1
              and "catalog.deploy_project" not in cap["procedures"] and cap["changed_on_server"] == ["Load_B.dtsx"]
              and cap["server_Load_B_is_source"] and cap["new_versions"] == 1)
        detail = (f"Rebuild + right-click Deploy Package in Visual Studio: calls {cap['procedures']}; changed "
                  f"{cap['changed_on_server']}; source deployed: {cap['server_Load_B_is_source']}; "
                  f"new versions: {cap['new_versions']} (wizard command line: vs-ui/c-review.png)")
        return ok, detail, json.dumps({"prepare": prep, "ui": ui, "capture": cap, "files": files}, indent=1)
    check("visual-studio-ui", visual_studio_ui)

    failed = [n for n, ok, _ in results if not ok]
    (out / "summary.json").write_text(json.dumps([{"check": n, "ok": ok, "detail": d} for n, ok, d in results], indent=1))
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    print(out)
    return not failed
