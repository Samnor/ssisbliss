"""Visual Studio's "Rebuild, then Deploy Package" vs ssisbliss, on the lab's SSISDB.

1. The corpus project (lab/tools/build-corpus.ps1, variant `configs`) is deployed as built by
   Visual Studio: the baseline in /SSISDB/PKGCMP/Corpus.
2. The developer edits Load_B.dtsx and runs Visual Studio's Rebuild (devenv /Rebuild): that
   writes obj/Development (re-saved packages, configuration applied) and bin/Development/Corpus.ispac.
3. Each method deploys only Load_B from the same starting point; an Extended Events session
   records the package bytes that reach the server. Visual Studio's Deploy Package runs the
   deployment wizard in package mode (ModelType Package), so the wizard is run that way with
   each possible source.

    lab.py run lab/tools/compare_package_deploy.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from ssisbliss.ssis.catalog import SqlCatalog, connection_string
from ssisbliss.ssis.dtsx import package_sha256
from ssisbliss.ssis.ispac import Ispac

SERVER = "localhost,14331"
FOLDER, PROJECT = "PKGCMP", "Corpus"
CORPUS = Path(r"C:\lab\work\corpus\configs")
WORK = Path(r"C:\lab\work\pkgdeploy")
VS = Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community")
WIZARD = VS / r"Common7\IDE\CommonExtensions\Microsoft\SSIS\170\Binn\ISDeploymentWizard.exe"
DEVENV = VS / r"Common7\IDE\devenv.com"
TOOL = Path(r"C:\lab\tools\venv\Scripts\ssisbliss.exe")
XE, APP = "ssisbliss_pkgdeploy", "ssisbliss-compare"
STATE = WORK / "state"   # this script's own base records (the tools keep them per user)
os.environ["SSISBLISS_STATE_DIR"] = str(STATE)
HEX = re.compile(r"0x([0-9A-Fa-f]{200,})")


class Server:
    def __init__(self):
        self.cat = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app=APP))

    def sql(self, q: str, *p):
        cur = self.cat.conn.cursor()
        cur.execute(q, *p)
        while cur.description is None and cur.nextset():
            pass
        return cur.fetchall() if cur.description else []

    def capture_start(self):
        self.sql(f"""
            IF EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = N'{XE}') DROP EVENT SESSION {XE} ON SERVER;
            CREATE EVENT SESSION {XE} ON SERVER
            ADD EVENT sqlserver.rpc_completed (ACTION (sqlserver.client_app_name) WHERE sqlserver.client_app_name <> N'{APP}'),
            ADD EVENT sqlserver.sql_batch_completed (ACTION (sqlserver.client_app_name) WHERE sqlserver.client_app_name <> N'{APP}')
            ADD TARGET package0.ring_buffer (SET max_memory = 102400) WITH (MAX_DISPATCH_LATENCY = 1 SECONDS);
            ALTER EVENT SESSION {XE} ON SERVER STATE = START;""")

    def capture_stop(self) -> list[dict]:
        time.sleep(3)
        rows = self.sql(f"""SELECT CAST(t.target_data AS nvarchar(max)) FROM sys.dm_xe_session_targets t
                            JOIN sys.dm_xe_sessions s ON s.address = t.event_session_address WHERE s.name = N'{XE}'""")
        self.sql(f"ALTER EVENT SESSION {XE} ON SERVER STATE = STOP; DROP EVENT SESSION {XE} ON SERVER;")
        out = []
        for ev in ET.fromstring(rows[0][0]).iter("event") if rows else []:
            d = {n.get("name", ""): n.findtext("value") for n in ev}
            text = d.get("statement") or d.get("batch_text") or ""
            procs = sorted({f"{a.lower()}.{b}" for a, b in re.findall(r"\[?\b(catalog|internal)\]?\.\[?(\w+)\]?", text, re.I)})
            if not procs:
                continue
            blobs = []
            for m in HEX.finditer(text):   # package bytes sent to the server
                data = bytes.fromhex(m.group(1))
                if data[:3] == b"\xef\xbb\xbf" or data.lstrip()[:1] == b"<":
                    blobs.append(data)
            out.append({"app": d.get("client_app_name"), "procedures": procs, "blobs": blobs})
        return out


def run(cmd, timeout=900):
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def edit(path: Path, note: str):
    text = path.read_text("utf-8-sig")
    text = re.sub(r'\s+DTS:Description="[^"]*"', "", text, count=1)
    path.write_text(text.replace('DTS:ObjectName="', f'DTS:Description="{note}"\n  DTS:ObjectName="', 1), "utf-8")


def main() -> int:
    srv = Server()
    if WORK.exists():
        shutil.rmtree(WORK)
    proj = WORK / "Corpus"
    shutil.copytree(CORPUS / "src", proj)

    # 1. Baseline: the project as Visual Studio built it, deployed.
    baseline = (CORPUS / "build-devenv" / "bin" / "Development" / "Corpus.ispac").read_bytes()
    srv.sql(f"IF NOT EXISTS (SELECT 1 FROM catalog.folders WHERE name = N'{FOLDER}') "
            f"EXEC catalog.create_folder @folder_name = N'{FOLDER}'")
    srv.cat.deploy_project(FOLDER, PROJECT, baseline)
    base_lsn = srv.cat.project(FOLDER, PROJECT).object_version_lsn
    base_hashes = Ispac(srv.cat.get_project(FOLDER, PROJECT)).package_hashes()
    run([TOOL, "pull", "--server", SERVER, "--trust-server-certificate", "--folder", FOLDER, "--project", PROJECT,
         "--out", proj])   # records your version per folder (not a git repository here)
    shutil.copytree(STATE, WORK / "state-baseline")

    # 2. The developer edits Load_B and rebuilds in Visual Studio.
    edit(proj / "Load_B.dtsx", "developer change")
    code, _ = run([DEVENV, proj / "Corpus.sln", "/Rebuild", "Development"])
    obj, bin_ispac = proj / "obj" / "Development", proj / "bin" / "Development" / "Corpus.ispac"
    rebuild = {"exit": code, "obj_files": sorted(p.name for p in obj.iterdir()) if obj.exists() else [],
               "ispac": bin_ispac.exists()}
    versions = {"source": package_sha256((proj / "Load_B.dtsx").read_bytes())}
    if (obj / "Load_B.dtsx").exists():
        versions["obj (Rebuild output)"] = package_sha256((obj / "Load_B.dtsx").read_bytes())
    if bin_ispac.exists():
        versions["bin .ispac (Rebuild output)"] = package_sha256(Ispac(bin_ispac.read_bytes()).package("Load_B.dtsx"))
    name_of = {v: k for k, v in versions.items()}

    target = ["/Silent", f"/DestinationServer:{SERVER}", f"/DestinationPath:/SSISDB/{FOLDER}/{PROJECT}"]
    methods = {
        "wizard package mode, source folder": [WIZARD, "/ModelType:Package", f"/SourcePath:{proj}", "/Packages:Load_B.dtsx", *target],
        "wizard package mode, obj folder": [WIZARD, "/ModelType:Package", f"/SourcePath:{obj}", "/Packages:Load_B.dtsx", *target],
        "wizard package mode, bin .ispac": [WIZARD, "/ModelType:Package", f"/SourcePath:{bin_ispac}", "/Packages:Load_B.dtsx", *target],
        "ssisbliss deploy-packages (source .dtsx)": [TOOL, "deploy-packages", "--server", SERVER, "--trust-server-certificate",
                                                       "--folder", FOLDER, "--project", PROJECT, proj / "Load_B.dtsx"],
        "ssisbliss deploy-project --dtproj --only": [TOOL, "deploy-project", "--server", SERVER, "--trust-server-certificate",
                                                       "--folder", FOLDER, "--project", PROJECT, "--dtproj", proj / "Corpus.dtproj",
                                                       "--only", "Load_B"],
    }
    results = []
    for label, cmd in methods.items():
        if srv.cat.project(FOLDER, PROJECT).object_version_lsn != base_lsn:
            srv.cat.restore_project(FOLDER, PROJECT, base_lsn)
        shutil.rmtree(STATE, ignore_errors=True)
        shutil.copytree(WORK / "state-baseline", STATE)
        srv.capture_start()
        if str(cmd[0]) == str(TOOL):
            code, out = run(cmd)
            try:
                plan = json.loads(out)
                if plan.get("plan_id"):
                    code, out = run([*cmd, "--apply", "--plan-id", plan["plan_id"], "--backup-dir", WORK / "bk"])
            except json.JSONDecodeError:
                pass
        else:
            code, out = run(cmd)
        events = srv.capture_stop()
        now = Ispac(srv.cat.get_project(FOLDER, PROJECT)).package_hashes()
        sent = [package_sha256(b) for e in events for b in e["blobs"]]
        params = srv.sql("""SELECT op.object_name + '::' + op.parameter_name FROM catalog.object_parameters op
                            JOIN catalog.projects p ON p.project_id = op.project_id JOIN catalog.folders f ON f.folder_id = p.folder_id
                            WHERE f.name = ? AND p.name = ? AND op.object_name = 'Load_B.dtsx' ORDER BY 1""", FOLDER, PROJECT)
        results.append({
            "method": label, "exit": code, "output": out[-500:],
            "changed_on_server": sorted(n for n in now if now[n] != base_hashes.get(n)),
            "server_Load_B_is": name_of.get(now.get("Load_B.dtsx"), "unchanged" if now.get("Load_B.dtsx") == base_hashes.get("Load_B.dtsx") else "other"),
            "bytes_sent_are": sorted({name_of.get(h, "other") for h in sent}),
            "procedures": sorted({p for e in events for p in e["procedures"] if p.startswith("catalog.")}),
            "Load_B_parameters": [r[0] for r in params],
            "stored": {n: now[n][:12] for n in sorted(now)},
        })
    if srv.cat.project(FOLDER, PROJECT).object_version_lsn != base_lsn:
        srv.cat.restore_project(FOLDER, PROJECT, base_lsn)
    # Do the methods that deployed leave the server with the same packages?
    deployed = [r for r in results if r["changed_on_server"]]
    same = {r["method"]: r["stored"] == deployed[0]["stored"] for r in deployed} if deployed else {}
    for r in results:
        r["output"] = r["output"][-300:]
    print(json.dumps({"rebuild": rebuild, "Load_B_versions": list(versions), "results": results,
                      "stored_result_same_as_first_method": same}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
