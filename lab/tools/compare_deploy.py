"""Compare Microsoft's deploy paths with ssisbliss on the lab's SSISDB.

Scenario, in catalog folder CMP (a copy of SILVER/SILVER):
  - baseline project with an environment reference and parameter bindings
  - a colleague deploys a work-in-progress change to Stage_Ledger
  - the developer's .ispac was built before that, with only Stage_Product changed
Each method starts from the same server state and is scored on: did the developer's change
land, did the colleague's work survive, did bindings / environment references survive, and
which SSISDB procedures it called (Extended Events capture).

Run on the lab host (lab.py run lab/tools/compare_deploy.py [--args ...]):
  --methods wizard,ssisdeploy,ssisbliss-project,ssisbliss-packages   (default: all)
  --observe MINUTES   reset the scenario, then capture whatever you deploy into CMP/SILVER from
                      Visual Studio over RDP for that long, and score it the same way
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from ssisbliss.ssis.catalog import SqlCatalog, connection_string
from ssisbliss.ssis.dtsx import as_editable, package_sha256
from ssisbliss.ssis.ispac import Ispac

SERVER = "localhost,14331"
SOURCE = ("SILVER", "SILVER")
FOLDER, PROJECT = "CMP", "SILVER"
WORK = Path(r"C:\lab\work\compare")
TOOLS = Path(r"C:\lab\tools")
TOOL = TOOLS / "venv" / "Scripts" / "ssisbliss.exe"
XE = "ssisbliss_compare"
APP = "ssisbliss-compare"
STATE = WORK / "state"                      # this script's own base records (the tools keep them per user)
os.environ["SSISBLISS_STATE_DIR"] = str(STATE)   # this script's own connection, excluded from the capture
CALL = re.compile(r"\[?\b(catalog|internal)\]?\.\[?(\w+)\]?", re.I)   # catalog.x and [catalog].[x]


def tool_paths() -> dict[str, str]:
    marker = TOOLS / "ide-installed.txt"
    found = dict(line.split("=", 1) for line in marker.read_text().splitlines() if "=" in line) if marker.exists() else {}
    if not found.get("wizard"):
        hits = [p for p in Path(r"C:\Program Files\Microsoft SQL Server").rglob("ISDeploymentWizard.exe") if "x86" not in str(p)]
        found["wizard"] = str(hits[0]) if hits else ""
    return found


class Lab:
    def __init__(self):
        self.cat = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app=APP))

    def sql(self, q: str, *params):
        cur = self.cat.conn.cursor()
        cur.execute(q, *params)
        while cur.description is None and cur.nextset():
            pass
        return cur.fetchall() if cur.description else []

    # ------------------------------------------------------------- scenario --
    def reset(self) -> dict:
        WORK.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(STATE, ignore_errors=True)
        shutil.rmtree(WORK / "state-baseline", ignore_errors=True)
        source = Ispac(self.cat.get_project(*SOURCE))
        self.sql(f"IF NOT EXISTS (SELECT 1 FROM catalog.folders WHERE name = N'{FOLDER}') "
                 f"EXEC catalog.create_folder @folder_name = N'{FOLDER}'")
        if self.cat.project(FOLDER, PROJECT):
            self.sql("EXEC catalog.delete_project @folder_name = ?, @project_name = ?", FOLDER, PROJECT)
        self.cat.deploy_project(FOLDER, PROJECT, source.data)
        self.sql(f"""
            IF NOT EXISTS (SELECT 1 FROM catalog.environments e JOIN catalog.folders f ON f.folder_id = e.folder_id
                           WHERE f.name = N'{FOLDER}' AND e.name = N'Default')
            BEGIN
                EXEC catalog.create_environment @folder_name = N'{FOLDER}', @environment_name = N'Default';
                EXEC catalog.create_environment_variable @folder_name = N'{FOLDER}', @environment_name = N'Default',
                     @variable_name = N'TargetDb', @data_type = N'String', @sensitive = 0, @value = N'SILVER';
            END
            DECLARE @ref bigint;
            EXEC catalog.create_environment_reference @folder_name = N'{FOLDER}', @project_name = N'{PROJECT}',
                 @environment_name = N'Default', @reference_type = 'R', @reference_id = @ref OUTPUT;
            EXEC catalog.set_object_parameter_value @object_type = 20, @folder_name = N'{FOLDER}',
                 @project_name = N'{PROJECT}', @parameter_name = N'TargetDb', @parameter_value = N'TargetDb',
                 @value_type = 'R';
            EXEC catalog.set_object_parameter_value @object_type = 20, @folder_name = N'{FOLDER}',
                 @project_name = N'{PROJECT}', @parameter_name = N'LoadMode', @parameter_value = N'Full',
                 @value_type = 'V';""")

        # The developer pulls Stage_Product (their base), then the colleague deploys Stage_Ledger.
        dev = WORK / "dev"
        if dev.exists():
            for f in dev.iterdir():
                f.unlink()
        run([str(TOOL), "pull", *target(), "--out", str(dev), "--packages", "Stage_Product.dtsx"])
        colleague = edit(as_editable(source.package("Stage_Ledger.dtsx")), "colleague work in progress")
        self.cat.deploy_packages(FOLDER, PROJECT, [("Stage_Ledger.dtsx", colleague)])

        # The developer's build: from before the colleague's change, with Stage_Product edited,
        # saved like a Visual Studio build (DontSaveSensitive, no ServerStorage).
        product = edit((dev / "Stage_Product.dtsx").read_bytes(), "developer change")
        (dev / "Stage_Product.dtsx").write_bytes(product)
        built = WORK / "SILVER.ispac"
        built.write_bytes(as_built(source, {"Stage_Product.dtsx": product}))
        p = self.cat.project(FOLDER, PROJECT)
        shutil.copytree(STATE, WORK / "state-baseline", dirs_exist_ok=True)
        return {"lsn": p.object_version_lsn, "hashes": self.hashes(), "bindings": self.bindings(),
                "env_refs": self.env_refs(), "built": str(built), "product": package_sha256(product),
                "colleague": package_sha256(colleague)}

    def restore(self, lsn: int) -> None:
        # Every method starts from the same server state and the same developer base record.
        shutil.rmtree(STATE, ignore_errors=True)
        shutil.copytree(WORK / "state-baseline", STATE)
        if self.cat.project(FOLDER, PROJECT).object_version_lsn != lsn:
            self.cat.restore_project(FOLDER, PROJECT, lsn)

    def hashes(self) -> dict[str, str]:
        return Ispac(self.cat.get_project(FOLDER, PROJECT)).package_hashes()

    def bindings(self) -> list:
        return [tuple(r) for r in self.sql(
            """SELECT op.parameter_name, op.value_type, op.referenced_variable_name,
                      CAST(op.default_value AS nvarchar(4000))
               FROM catalog.object_parameters op JOIN catalog.projects p ON p.project_id = op.project_id
               JOIN catalog.folders f ON f.folder_id = p.folder_id
               WHERE f.name = ? AND p.name = ? AND op.value_set = 1 ORDER BY 1""", FOLDER, PROJECT)]

    def env_refs(self) -> list:
        return [tuple(r) for r in self.sql(
            """SELECT r.environment_name, r.reference_type FROM catalog.environment_references r
               JOIN catalog.projects p ON p.project_id = r.project_id JOIN catalog.folders f ON f.folder_id = p.folder_id
               WHERE f.name = ? AND p.name = ? ORDER BY 1""", FOLDER, PROJECT)]

    # ---------------------------------------------------------------- capture --
    def capture_start(self) -> None:
        self.sql(f"""
            IF EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = N'{XE}') DROP EVENT SESSION {XE} ON SERVER;
            CREATE EVENT SESSION {XE} ON SERVER
            ADD EVENT sqlserver.rpc_completed (ACTION (sqlserver.client_app_name, sqlserver.username, sqlserver.database_name)
                WHERE sqlserver.client_app_name <> N'{APP}' AND NOT sqlserver.like_i_sql_unicode_string(sqlserver.client_app_name, N'SQLAgent%')),
            ADD EVENT sqlserver.sql_batch_completed (ACTION (sqlserver.client_app_name, sqlserver.username, sqlserver.database_name)
                WHERE sqlserver.client_app_name <> N'{APP}' AND NOT sqlserver.like_i_sql_unicode_string(sqlserver.client_app_name, N'SQLAgent%'))
            ADD TARGET package0.ring_buffer (SET max_memory = 51200)
            WITH (MAX_DISPATCH_LATENCY = 1 SECONDS);
            ALTER EVENT SESSION {XE} ON SERVER STATE = START;""")

    def capture_stop(self) -> list[dict]:
        time.sleep(3)
        rows = self.sql(f"""SELECT CAST(t.target_data AS nvarchar(max)) FROM sys.dm_xe_session_targets t
                            JOIN sys.dm_xe_sessions s ON s.address = t.event_session_address WHERE s.name = N'{XE}'""")
        self.sql(f"ALTER EVENT SESSION {XE} ON SERVER STATE = STOP; DROP EVENT SESSION {XE} ON SERVER;")
        events = []
        for ev in ET.fromstring(rows[0][0]).iter("event") if rows else []:
            d = {"event": ev.get("name")}
            for node in ev:
                d[node.get("name", "")] = node.findtext("value")
            text = d.get("statement") or d.get("batch_text") or ""
            if CALL.search(text):
                events.append({"app": d.get("client_app_name"), "user": d.get("username"),
                               "database": d.get("database_name"), "sql": shorten(text)})
        return events


def target() -> list[str]:
    return ["--server", SERVER, "--trust-server-certificate", "--folder", FOLDER, "--project", PROJECT]


def run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return p.returncode, (p.stdout + p.stderr).strip()


def edit(data: bytes, note: str) -> bytes:
    text = data.decode("utf-8-sig")
    text = re.sub(r'(<DTS:Executable[^>]*?)\s+DTS:Description="[^"]*"', r"\1", text, count=1)
    return text.replace("<DTS:Executable ", f'<DTS:Executable DTS:Description="{note} {time.time():.0f}" ', 1).encode()


def as_built(source: Ispac, replace: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source.data)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item)
            name = item.filename
            if name.lower().endswith(".dtsx"):
                data = replace[name] if name in replace else as_editable(data)
            elif name == "@Project.manifest":
                data = data.replace(b'SSIS:ProtectionLevel="ServerStorage"', b'SSIS:ProtectionLevel="DontSaveSensitive"', 1)
            zout.writestr(item, data)
    return out.getvalue()


def shorten(sql: str) -> str:
    sql = re.sub(r"0x[0-9A-Fa-f]{64,}", lambda m: f"0x<{(len(m.group()) - 2) // 2} bytes>", sql)
    sql = re.sub(r"\s+", " ", sql).strip()
    return sql if len(sql) <= 600 else sql[:600] + " ..."


def score(lab: Lab, base: dict, name: str, code: int | None, output: str, events: list[dict]) -> dict:
    now = lab.hashes()
    changed = sorted(n for n in set(now) | set(base["hashes"]) if now.get(n) != base["hashes"].get(n))
    calls = []
    for e in events:
        for schema, proc in CALL.findall(e["sql"]):
            c = f"{schema.lower()}.{proc}"
            if c.lower() not in [x.lower() for x in calls]:
                calls.append(c)
    return {
        "method": name, "exit": code, "output": output[-1500:],
        "developer_change_deployed": now.get("Stage_Product.dtsx") == base["product"],
        "colleague_work_kept": now.get("Stage_Ledger.dtsx") == base["colleague"],
        "packages_changed": changed,
        "bindings_kept": lab.bindings() == base["bindings"],
        "environment_references_kept": lab.env_refs() == base["env_refs"],
        "new_lsn": lab.cat.project(FOLDER, PROJECT).object_version_lsn, "baseline_lsn": base["lsn"],
        "procedures_called": calls, "statements": events[:40],
    }


def method_commands(base: dict, tools: dict) -> dict[str, list[list[str]] | str]:
    built = base["built"]
    return {
        "wizard": [[tools.get("wizard", ""), "/Silent", "/ModelType:Project", f"/SourcePath:{built}",
                    f"/DestinationServer:{SERVER}", f"/DestinationPath:/SSISDB/{FOLDER}/{PROJECT}"]],
        "ssisdeploy": [[str(Path(tools.get("ssisdevops", r"C:\lab\tools\ssisdevops")) / "SSISDeploy.exe"),
                        f"-s:{built}", f"-d:catalog;/SSISDB/{FOLDER}/{PROJECT};{SERVER}", "-at:win",
                        "-css:TrustServerCertificate=True"]],
        "ssisbliss-project": "deploy-project",
        "ssisbliss-packages": "deploy-packages",
    }


def run_ssisbliss(kind: str, base: dict) -> tuple[int, str]:
    if kind == "deploy-project":
        args = ["deploy-project", *target(), "--ispac", base["built"], "--only", "Stage_Product",
                "--base-dir", str(WORK / "dev")]
    else:
        args = ["deploy-packages", *target(), str(WORK / "dev" / "Stage_Product.dtsx")]
    code, out = run([str(TOOL), *args])
    try:
        plan = json.loads(out)
    except json.JSONDecodeError:
        return code, out
    if not plan.get("plan_id"):
        return code, out
    return run([str(TOOL), *args, "--apply", "--plan-id", plan["plan_id"], "--backup-dir", str(WORK / "bk")])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", default="wizard,ssisdeploy,ssisbliss-project,ssisbliss-packages")
    ap.add_argument("--observe", type=float, help="minutes to capture a deploy you do from Visual Studio")
    a = ap.parse_args()
    lab, tools = Lab(), tool_paths()
    base = lab.reset()
    results = []
    if a.observe:
        print(f"Scenario ready in /SSISDB/{FOLDER}/{PROJECT} (lsn {base['lsn']}). Deploy from Visual Studio now; "
              f"capturing for {a.observe} minutes.", flush=True)
        lab.capture_start()
        time.sleep(a.observe * 60)
        results.append(score(lab, base, "observed (Visual Studio)", None, "", lab.capture_stop()))
    else:
        commands = method_commands(base, tools)
        for name in a.methods.split(","):
            lab.restore(base["lsn"])
            lab.capture_start()
            spec = commands[name]
            if isinstance(spec, str):
                code, out = run_ssisbliss(spec, base)
            elif not spec[0][0] or not Path(spec[0][0]).exists():
                code, out = None, f"tool not installed: {spec[0][0] or name}"
            else:
                code, out = run(spec[0])
            results.append(score(lab, base, name, code, out, lab.capture_stop()))
        lab.restore(base["lsn"])
    doc = {"scenario": {"folder": FOLDER, "project": PROJECT, "baseline_lsn": base["lsn"],
                        "developer_changed": "Stage_Product.dtsx", "colleague_changed_on_server": "Stage_Ledger.dtsx"},
           "results": results}
    (WORK / "result.json").write_text(json.dumps(doc, indent=2))
    # SSM returns at most 24 KB of output: print the scores; statements stay in result.json.
    brief = {**doc, "results": [{k: v for k, v in r.items() if k not in ("statements", "output")}
                                | {"output": r["output"][-400:]} for r in results]}
    print(json.dumps(brief, indent=2))
    print(f"full result with captured statements: {WORK / 'result.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
