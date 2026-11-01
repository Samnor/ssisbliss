"""`ssisbliss deps` across a medallion estate on the real SQL Server (medallion-corpus.ps1): three projects, a
database per layer, a dbo.Customer in each. Each project's database parameters are bound to SSIS environment
variables, as production estates do, while their design value stays DESIGN; so the tool must read the
environments to tell the layers apart. From SILVER's Load_Customer, the graph must hold exactly the real links
across the layers and none through another layer's dbo.Customer. Before the binding, every connection says
DESIGN, and the run shows what that would merge (kept for the record, not judged)."""
import json
import subprocess
import time
from pathlib import Path

from ssisbliss.ssis.catalog import SqlCatalog, connection_string

SERVER = "localhost,14331"
TOOL = r"C:\lab\tools\venv\Scripts\ssisbliss.exe"
DIR = Path(r"C:\lab\work\medallion")
LAYERS = {"BRONZE": ("MEDBRONZE", {"TargetDb": "LABBRONZE"}),
          "SILVER": ("MEDSILVER", {"TargetDb": "LABSILVER", "SourceDb": "LABBRONZE"}),
          "GOLD": ("MEDGOLD", {"TargetDb": "LABGOLD"})}
EXPECTED = {("BRONZE/Load_Customer.dtsx", "SILVER/Load_Customer.dtsx", ("labbronze.dbo.customer",)),
            ("SILVER/Load_Customer.dtsx", "SILVER/Load_Order.dtsx", ("labsilver.dbo.customer",)),
            ("SILVER/Load_Customer.dtsx", "GOLD/Dim_Customer.dtsx", ("labsilver.dbo.customer",)),
            ("GOLD/Dim_Customer.dtsx", "GOLD/Load_Customer.dtsx", ("labgold.dbo.dimcustomer",))}
T = ["--server", SERVER, "--trust-server-certificate"]
c = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app="ssisbliss-compare"))


def sql(statement: str, *args) -> None:
    c.conn.cursor().execute(statement, *args)


def run(*args: str) -> tuple[dict | str, float]:
    t = time.perf_counter()
    p = subprocess.run([TOOL, *args], capture_output=True, text=True, encoding="utf-8")
    took = time.perf_counter() - t
    return (json.loads(p.stdout) if p.stdout.lstrip().startswith("{") else p.stdout or p.stderr), took


def graph() -> tuple[dict | str, float]:
    return run("deps", "Load_Customer", *T, "--folder", "MEDSILVER", "--project", "SILVER",
               "--also", "MEDBRONZE/BRONZE", "--also", "MEDGOLD/GOLD")


def short(key: str) -> str:   # FOLDER/PROJECT/Pkg.dtsx (projects that share a name) -> PROJECT/Pkg.dtsx
    return "/".join(key.split("/")[-2:])


def edges(doc) -> set:
    return {(short(e["from"]), short(e["to"]), tuple(e.get("tables", []))) for e in doc.get("edges", [])} \
        if isinstance(doc, dict) else set()


def cleanup() -> None:
    for project, (folder, _) in LAYERS.items():
        sql(f"""IF EXISTS (SELECT 1 FROM catalog.projects p JOIN catalog.folders f ON f.folder_id = p.folder_id
                           WHERE f.name = N'{folder}' AND p.name = N'{project}')
                    EXEC catalog.delete_project @folder_name = N'{folder}', @project_name = N'{project}'""")
        sql(f"""IF EXISTS (SELECT 1 FROM catalog.environments e JOIN catalog.folders f ON f.folder_id = e.folder_id
                           WHERE f.name = N'{folder}' AND e.name = N'Layer')
                    EXEC catalog.delete_environment @folder_name = N'{folder}', @environment_name = N'Layer'""")
        sql(f"IF EXISTS (SELECT 1 FROM catalog.folders WHERE name = N'{folder}') EXEC catalog.delete_folder @folder_name = N'{folder}'")


cleanup()
result: dict = {}
try:
    for project, (folder, _) in LAYERS.items():
        sql(f"EXEC catalog.create_folder @folder_name = N'{folder}'")
        c.deploy_project(folder, project, (DIR / f"{project}.ispac").read_bytes())
    before, _ = graph()
    result["before_binding"] = sorted(map(list, edges(before))) if isinstance(before, dict) else str(before)[:600]

    for project, (folder, values) in LAYERS.items():
        sql("EXEC catalog.create_environment @folder_name = ?, @environment_name = N'Layer'", folder)
        for name, value in values.items():
            sql("""EXEC catalog.create_environment_variable @folder_name = ?, @environment_name = N'Layer',
                        @variable_name = ?, @data_type = N'String', @sensitive = 0, @value = ?""",
                folder, f"{name}Var", value)
            sql("""DECLARE @id bigint;
                   IF NOT EXISTS (SELECT 1 FROM catalog.environment_references r JOIN catalog.projects p
                                  ON p.project_id = r.project_id WHERE p.name = ? AND r.environment_name = N'Layer')
                       EXEC catalog.create_environment_reference @folder_name = ?, @project_name = ?,
                            @environment_name = N'Layer', @reference_type = 'R', @reference_id = @id OUTPUT""",
                project, folder, project)
            sql("""EXEC catalog.set_object_parameter_value @object_type = 20, @folder_name = ?, @project_name = ?,
                        @parameter_name = ?, @parameter_value = ?, @value_type = 'R'""",
                folder, project, name, f"{name}Var")

    after, took = graph()
    got = edges(after)
    result["graph"] = {
        "edges": sorted(map(list, got)), "missing": sorted(map(list, EXPECTED - got)), "extra": sorted(map(list, got - EXPECTED)),
        "unsure": [e for e in after.get("edges", []) if e.get("unsure")] if isinstance(after, dict) else None,
        "unknown_database": [n["package"] for n in after.get("nodes", []) if n.get("unknown_database")]
        if isinstance(after, dict) else None,
        "order": after.get("order") if isinstance(after, dict) else str(after)[:600],
        "seconds": round(took, 2)}
    everything, took_all = run("deps", "Load_Customer", *T, "--folder", "MEDSILVER", "--project", "SILVER", "--all-projects")
    result["all_projects"] = {"ok": isinstance(everything, dict) and everything.get("ok"),
                              "projects": len(everything.get("projects", [])) if isinstance(everything, dict) else str(everything)[:300],
                              "same_edges": edges(everything) == got, "seconds": round(took_all, 2),
                              "package": everything.get("package") if isinstance(everything, dict) else None,
                              "more": sorted(map(list, edges(everything) - got))}
finally:
    cleanup()

g = result.get("graph", {})
result["ok"] = bool(g and not g["missing"] and not g["extra"] and not g["unsure"] and not g["unknown_database"]
                    and g["seconds"] < 15 and result["all_projects"]["ok"] and result["all_projects"]["same_edges"])
print(json.dumps(result, indent=1))
