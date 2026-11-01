"""`ssisbliss mcp` against the real SQL Server, driven as an MCP client would over stdio, in a scratch copy of
SILVER (folder MCPCHECK). Pull, change a package, plan, an apply without the plan id (refused, its `next`
carries the plan id), the apply, status after it, and a read-only chain: each a tools/call with the output
contract (ok, error_kind, next) in structuredContent."""
import json
import re
import shutil
import subprocess
from pathlib import Path

from ssisbliss.ssis.catalog import SqlCatalog, connection_string

SERVER, F, P = "localhost,14331", "MCPCHECK", "SILVER"
TOOL = r"C:\lab\tools\venv\Scripts\ssisbliss.exe"
WORK = Path(r"C:\lab\work\mcp")
c = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app="ssisbliss-compare"))
c.conn.cursor().execute(f"IF NOT EXISTS (SELECT 1 FROM catalog.folders WHERE name = N'{F}') EXEC catalog.create_folder @folder_name = N'{F}'")
if c.project(F, P):
    c.conn.cursor().execute("EXEC catalog.delete_project @folder_name = ?, @project_name = ?", F, P)
c.deploy_project(F, P, c.get_project("SILVER", "SILVER"))
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)

server = subprocess.Popen([TOOL, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
ids = iter(range(1, 1000))


def rpc(method: str, params: dict | None = None) -> dict:
    mid = next(ids)
    server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n")
    server.stdin.flush()
    answer = json.loads(server.stdout.readline())
    assert answer["id"] == mid, answer
    return answer.get("result") or answer


def call(tool: str, **args) -> dict:
    r = rpc("tools/call", {"name": tool, "arguments": {"server": SERVER, "trust_server_certificate": True, **args}})
    doc = r.get("structuredContent") or {}
    return {"is_error": r.get("isError"), **doc}


T = {"folder": F, "project": P}
init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "lab", "version": "1"}})
server.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
tools = {t["name"]: t for t in rpc("tools/list")["tools"]}
result: dict = {"protocol": init.get("protocolVersion"), "tools": len(tools),
                "rules_in_instructions": "Never commit, push" in init.get("instructions", ""),
                "deploy_is_destructive": tools["deploy_packages"]["annotations"]["destructiveHint"],
                "chain_is_read_only": tools["chain"]["annotations"]["readOnlyHint"]}
try:
    pulled = call("pull", **T, out=str(WORK), packages=["Stage_Customer.dtsx"])
    result["pull"] = pulled.get("ok")
    pkg = WORK / "Stage_Customer.dtsx"
    data = pkg.read_bytes()
    # A real change (the hash ignores volatile attributes such as VersionGUID): the package's description.
    root = re.search(rb"<DTS:Executable[^>]*>", data)
    tag = root.group(0)
    new_tag = (re.sub(rb'DTS:Description="[^"]*"', b'DTS:Description="lab mcp check"', tag) if b"DTS:Description=" in tag
               else tag.replace(b"<DTS:Executable", b'<DTS:Executable DTS:Description="lab mcp check"', 1))
    changed = data[:root.start()] + new_tag + data[root.end():]
    assert changed != data, "the description did not change"
    pkg.write_bytes(changed)
    deploy = {**T, "files": [str(pkg)], "backup_dir": str(WORK / "bk")}
    plan = call("deploy_packages", **deploy)
    result["plan"] = {k: plan.get(k) for k in ("ok", "can_apply", "next", "apply_when")}
    no_id = call("deploy_packages", **deploy, apply=True)
    result["apply_without_plan_id"] = {k: no_id.get(k) for k in ("ok", "error_kind", "is_error", "next")}
    applied = call("deploy_packages", **deploy, apply=True, plan_id=plan.get("plan_id"))
    result["apply"] = {k: applied.get(k) for k in ("ok", "status", "new_lsn")}
    status = call("status", **T, dir=str(WORK))
    result["status_after"] = next((r["server"] for r in status.get("packages", []) if r["package"] == "Stage_Customer.dtsx"), None)
    chain = call("chain")
    result["chain"] = {"ok": chain.get("ok"), "chains": len(chain.get("chains", []))}
    bad = call("rollback", **T)
    result["missing_argument"] = {k: bad.get(k) for k in ("ok", "error_kind", "next")}
finally:
    server.stdin.close()
    server.wait(timeout=30)
    c.conn.cursor().execute("EXEC catalog.delete_project @folder_name = ?, @project_name = ?", F, P)

pid = (result.get("plan") or {}).get("next") or [""]
result["ok"] = bool(
    result["rules_in_instructions"] and result["deploy_is_destructive"] and result["chain_is_read_only"]
    and result.get("pull") and result["plan"]["ok"] and result["plan"]["can_apply"] and "--apply --plan-id" in pid[0]
    and result["apply_without_plan_id"]["error_kind"] == "refused" and result["apply_without_plan_id"]["is_error"]
    and result["apply_without_plan_id"]["next"] == pid
    and result["apply"]["ok"] and str(result["status_after"]).startswith("as you left it")
    and result["chain"]["ok"] and result["missing_argument"]["error_kind"] == "usage"
    and result["missing_argument"]["next"] == ["ssisbliss commands rollback"])
print(json.dumps(result, indent=1, default=str))
