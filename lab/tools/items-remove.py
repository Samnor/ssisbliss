"""Per-item project parameter deploys and remove-packages against the real SSISDB, in a scratch copy of
SILVER (folder ITEMS). A colleague's parameter must survive yours; a package you added must go cleanly."""
import io, json, re, subprocess, sys, zipfile
from pathlib import Path
from ssisbliss.ssis.catalog import SqlCatalog, connection_string
from ssisbliss.ssis.ispac import Ispac
from ssisbliss.ssis.dtsx import as_editable

SERVER, F, P = "localhost,14331", "ITEMS", "SILVER"
TOOL = r"C:\lab\tools\venv\Scripts\onprem-ssis.exe"
WORK = Path(r"C:\lab\work\items")
c = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app="onprem-compare"))
c.conn.cursor().execute(f"IF NOT EXISTS (SELECT 1 FROM catalog.folders WHERE name = N'{F}') EXEC catalog.create_folder @folder_name = N'{F}'")
if c.project(F, P):
    c.conn.cursor().execute("EXEC catalog.delete_project @folder_name = ?, @project_name = ?", F, P)
c.deploy_project(F, P, c.get_project("SILVER", "SILVER"))
T = ["--server", SERVER, "--trust-server-certificate", "--folder", F, "--project", P]


def run(*args):
    p = subprocess.run([TOOL, *map(str, args)], capture_output=True, text=True)
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, {"raw": p.stdout[-500:] + p.stderr[-500:]}


def plan_apply(*args):
    code, plan = run(*args)
    if not plan.get("plan_id"):
        return code, plan
    return run(*args, "--apply", "--plan-id", plan["plan_id"], "--backup-dir", WORK / "bk")


def with_param(data: bytes, name: str, value: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i in zin.infolist():
            part = zin.read(i)
            if i.filename == "Project.params":
                text = part.decode("utf-8-sig")
                new = (f'<SSIS:Parameter SSIS:Name="{name}"><SSIS:Properties><SSIS:Property SSIS:Name="ID">{{11111111-2222-3333-4444-'
                       f'{abs(hash(name)) % 10**12:012d}}}</SSIS:Property><SSIS:Property SSIS:Name="CreationName"></SSIS:Property>'
                       '<SSIS:Property SSIS:Name="Description"></SSIS:Property><SSIS:Property SSIS:Name="IncludeInDebugDump">0'
                       '</SSIS:Property><SSIS:Property SSIS:Name="Required">0</SSIS:Property><SSIS:Property SSIS:Name="Sensitive">0'
                       f'</SSIS:Property><SSIS:Property SSIS:Name="Value">{value}</SSIS:Property><SSIS:Property SSIS:Name="DataType">18'
                       '</SSIS:Property></SSIS:Properties></SSIS:Parameter>')
                part = text.replace("</SSIS:Parameters>", new + "</SSIS:Parameters>").encode()
            z.writestr(i, part)
    return out.getvalue()


def params():
    return sorted(r["parameter_name"] for r in c._rows(
        """SELECT op.parameter_name FROM catalog.object_parameters op JOIN catalog.projects p ON p.project_id = op.project_id
           JOIN catalog.folders f ON f.folder_id = p.folder_id WHERE f.name = ? AND p.name = ? AND op.object_type = 20
           AND op.parameter_name NOT LIKE 'CM.%'""", F, P))


result = {}
proj = WORK / P
if WORK.exists():
    import shutil
    shutil.rmtree(WORK)
run("pull", *T, "--out", proj)
deployed = c.get_project(F, P)
# A colleague adds Region (their own build, deployed with --item from another machine).
c.deploy_project(F, P, Ispac(deployed).merged_items(Ispac(with_param(deployed, "Region", "EU")), [], ["param:Region"]))
result["after_colleague"] = params()
# My build adds BatchSize; deploy only that item.
built = proj / "bin" / "Development" / f"{P}.ispac"
built.parent.mkdir(parents=True)
built.write_bytes(with_param(deployed, "BatchSize", "500"))
code, out = plan_apply("deploy-project", *T, "--ispac", built, "--item", "BatchSize")
result["item_deploy"] = out.get("status") or out.get("checks") or out
result["after_mine"] = params()
# A package my branch added, then removed again.
new = proj / "Stage_Extra.dtsx"
data = as_editable(Ispac(deployed).package("Stage_Customer.dtsx"))   # bytes: no Windows newline translation
data = re.sub(rb'DTS:DTSID="\{[^}]+\}"', b'DTS:DTSID="{9A9A9A9A-0000-4000-8000-000000000001}"', data, count=1)
new.write_bytes(data.replace(b'DTS:ObjectName="Stage_Customer"', b'DTS:ObjectName="Stage_Extra"', 1))
code, out = plan_apply("deploy-packages", *T, new, "--allow-new")
result["add_package"] = out.get("status")
code, out = plan_apply("remove-packages", *T, "Stage_Extra", "--dir", proj)
result["remove_package"] = out.get("status") or out
result["packages_now"] = sorted(Ispac(c.get_project(F, P)).package_names)
print(json.dumps(result, indent=1, default=str))
ok = (result["after_mine"] == sorted(["LoadMode", "TargetDb", "TargetServer", "Region", "BatchSize"])
      and result["item_deploy"] == "verified" and result["add_package"] == "verified" and result["remove_package"] == "verified"
      and "Stage_Extra.dtsx" not in result["packages_now"])
print("ITEMS-REMOVE PASSED" if ok else "ITEMS-REMOVE FAILED")
