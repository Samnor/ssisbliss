"""`slow` and `lint --env`'s provider check against the real SQL Server. SILVER's Stage_Ledger runs three
times as usual and once with a trigger that makes its one task wait 4 seconds; `slow` must name that task
from catalog.executable_statistics. The Excel providers a package uses are checked against the server's
own xp_enum_oledb_providers (the lab has no Access Database Engine, so both are missing)."""
import json

from ssisbliss.ssis.catalog import SqlCatalog, connection_string
from ssisbliss.ssis.cli import _oledb_providers
from ssisbliss.ssis.design import missing_providers
from ssisbliss.ssis.dtsx import DTS
from ssisbliss.ssis.timing import report

SERVER = "localhost,14331"
FOLDER = PROJECT = "SILVER"
PACKAGE = "Stage_Ledger.dtsx"
c = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app="ssisbliss-compare"))
# the environment SQL Agent's jobs run SILVER with (it sets TargetServer and TargetDb)
REFERENCE = c.environment_references(c.project(FOLDER, PROJECT).project_id)[0]["reference_id"]


def run(sql: str, *params) -> None:
    """SQL in SILVER; the connection goes back to SSISDB, where the catalog's procedures are."""
    cur = c.conn.cursor()
    try:
        cur.execute(f"{sql}; USE SSISDB", *params)
        while cur.nextset():
            pass
    finally:
        cur.close()


def execute() -> int:
    cur = c.conn.cursor()
    try:
        return _start(cur)
    finally:
        cur.close()


def _start(cur) -> int:
    cur.execute("""SET NOCOUNT ON; DECLARE @id bigint;
        EXEC catalog.create_execution @folder_name = ?, @project_name = ?, @package_name = ?, @reference_id = ?,
             @execution_id = @id OUTPUT;
        EXEC catalog.set_execution_parameter_value @id, @object_type = 50, @parameter_name = N'SYNCHRONIZED', @parameter_value = 1;
        EXEC catalog.start_execution @id;
        SELECT @id""", FOLDER, PROJECT, PACKAGE, REFERENCE)
    while cur.description is None and cur.nextset():
        pass
    return int(cur.fetchone()[0])


run("USE SILVER; IF OBJECT_ID('dbo.LabSlow') IS NULL CREATE TABLE dbo.LabSlow (On_ bit);"
    "DELETE dbo.LabSlow; DROP TRIGGER IF EXISTS dbo.LabSlowLedger;")
run("USE SILVER; EXEC(N'CREATE TRIGGER dbo.LabSlowLedger ON dbo.LoadLog AFTER INSERT AS "
    "IF EXISTS (SELECT 1 FROM dbo.LabSlow) AND EXISTS (SELECT 1 FROM inserted WHERE Package = N''Stage_Ledger'') "
    "WAITFOR DELAY ''00:00:04'';')")
try:
    usual = [execute() for _ in range(3)]
    run("USE SILVER; INSERT dbo.LabSlow VALUES (1)")
    slow_id = execute()
finally:
    run("USE SILVER; DROP TRIGGER IF EXISTS dbo.LabSlowLedger; DROP TABLE IF EXISTS dbo.LabSlow")
r = report(c.task_durations(FOLDER, PROJECT, PACKAGE, 20, up_to=slow_id), slow_id)

providers = _oledb_providers(c.conn)
excel = f"""<?xml version="1.0"?><DTS:Executable xmlns:DTS="{DTS}" DTS:refId="Package" DTS:ObjectName="X">
<DTS:ConnectionManagers>
<DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[Prices]" DTS:ObjectName="Prices" DTS:CreationName="EXCEL">
<DTS:ObjectData><DTS:ConnectionManager DTS:ConnectionString="Provider=Microsoft.ACE.OLEDB.16.0;Data Source=C:\\in\\p.xlsx;" />
</DTS:ObjectData></DTS:ConnectionManager>
<DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[Old]" DTS:ObjectName="Old" DTS:CreationName="EXCEL">
<DTS:ObjectData><DTS:ConnectionManager DTS:ConnectionString="Provider=Microsoft.Jet.OLEDB.4.0;Data Source=C:\\in\\o.xls;" />
</DTS:ObjectData></DTS:ConnectionManager>
</DTS:ConnectionManagers></DTS:Executable>""".encode()
missing = [f.task for f in missing_providers("X.dtsx", excel, providers or set(), "lab")]
result = {"usual_runs": usual, "slow_run": slow_id, "slow": r, "providers": sorted(providers or []),
          "missing": missing}
first = (r.get("slower_tasks") or [{}])[0]
result["ok"] = (first.get("task") == "Load Stage_Ledger" and first.get("extra_s", 0) >= 3
                and providers is not None and "MSOLEDBSQL" in providers and sorted(missing) == ["Old", "Prices"])
print(json.dumps(result, indent=1, default=str))
