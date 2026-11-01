"""`ssisbliss deps` on the real SQL Server. The DEPS project (deps-corpus.ps1: Execute Package tasks, a data flow
and Execute SQL tasks saved by the SSIS object model) is deployed to folder DEPSCHECK; from Clean.dtsx the
graph must hold exactly its real links, read from the catalog and from the same packages pulled to disk, with
the expression-built SQL marked and the unrelated package left out. Every deployed SILVER package must graph
without an error too, and it must be quick."""
import json
import shutil
import subprocess
import time
from pathlib import Path

from ssisbliss.ssis.catalog import SqlCatalog, connection_string

SERVER = "localhost,14331"
TOOL = r"C:\lab\tools\venv\Scripts\ssisbliss.exe"
ISPAC = Path(r"C:\lab\work\deps\DEPS.ispac")
WORK = Path(r"C:\lab\work\deps-run")
F, P = "DEPSCHECK", "DEPS"
EXPECTED_EDGES = {("Master.dtsx", "Extract.dtsx", "runs", ()), ("Master.dtsx", "Clean.dtsx", "runs", ()),
                  ("Extract.dtsx", "Clean.dtsx", "tables", ("labdeps.dbo.raworders",)),
                  ("Clean.dtsx", "Report.dtsx", "tables", ("labdeps.dbo.cleanorders",)),
                  ("Report.dtsx", "Audit.dtsx", "tables", ("labdeps.dbo.dailytotals",))}


def target(folder: str, project: str) -> list[str]:
    return ["--server", SERVER, "--trust-server-certificate", "--folder", folder, "--project", project]


def run(*args: str) -> tuple[dict | str, float]:
    t = time.perf_counter()
    p = subprocess.run([TOOL, *args], capture_output=True, text=True, encoding="utf-8", cwd=WORK)
    took = time.perf_counter() - t
    return (json.loads(p.stdout) if p.stdout.lstrip().startswith("{") else p.stdout or p.stderr), took


def edges(doc: dict) -> set:
    return {(e["from"], e["to"], e["kind"], tuple(e.get("tables", []))) for e in doc.get("edges", [])}


c = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app="ssisbliss-compare"))
c.conn.cursor().execute(f"IF NOT EXISTS (SELECT 1 FROM catalog.folders WHERE name = N'{F}') EXEC catalog.create_folder @folder_name = N'{F}'")
c.deploy_project(F, P, ISPAC.read_bytes())
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
result: dict = {}
try:
    pulled, _ = run("pull", *target(F, P), "--out", str(WORK / "deps"))
    server, took = run("deps", "Clean", *target(F, P))
    local, _ = run("deps", str(WORK / "deps" / "Clean.dtsx"))
    mermaid, _ = run("deps", "Clean", *target(F, P), "--format", "mermaid")
    ok_docs = isinstance(server, dict) and server.get("ok") and isinstance(local, dict) and local.get("ok")
    result["deps"] = {
        "pulled": isinstance(pulled, dict) and pulled.get("ok"),
        "nodes": {n["package"]: n["role"] for n in server.get("nodes", [])} if ok_docs else str(server)[:600],
        "edges": sorted(map(list, edges(server))) if ok_docs else str(local)[:600],
        "missing_edges": sorted(map(list, EXPECTED_EDGES - edges(server))) if ok_docs else None,
        "extra_edges": sorted(map(list, edges(server) - EXPECTED_EDGES)) if ok_docs else None,
        "unknown_sql": [n["package"] for n in server.get("nodes", []) if n.get("unknown_sql")] if ok_docs else None,
        "order": server.get("order") if ok_docs else None,
        "catalog_and_files_agree": bool(ok_docs and edges(server) == edges(local)),
        "seconds": round(took, 2),
        "mermaid": mermaid if isinstance(mermaid, str) else str(mermaid)[:300],
    }
finally:
    c.conn.cursor().execute("EXEC catalog.delete_project @folder_name = ?, @project_name = ?", F, P)

# every deployed SILVER package (built like production packages) graphs without an error
pulled, _ = run("pull", *target("SILVER", "SILVER"), "--out", str(WORK / "silver"))
silver = sorted(p.name for p in (WORK / "silver").glob("*.dtsx"))
errors, slowest = {}, 0.0
for name in silver:
    doc, took = run("deps", name, *target("SILVER", "SILVER"))
    slowest = max(slowest, took)
    if not (isinstance(doc, dict) and doc.get("ok")):
        errors[name] = str(doc)[:300]
result["silver"] = {"packages": len(silver), "errors": errors, "slowest_seconds": round(slowest, 2)}

d = result["deps"]
result["ok"] = bool(
    d["catalog_and_files_agree"] and not d["missing_edges"] and not d["extra_edges"]
    and set(d["nodes"]) == {"Master.dtsx", "Extract.dtsx", "Clean.dtsx", "Report.dtsx", "Audit.dtsx"}
    and d["unknown_sql"] == ["Audit.dtsx"] and d["order"] and d["order"].index("Master.dtsx") < d["order"].index("Clean.dtsx")
    and str(d["mermaid"]).startswith("flowchart LR") and d["seconds"] < 15
    and silver and not errors and slowest < 15)
print(json.dumps(result, indent=1))
