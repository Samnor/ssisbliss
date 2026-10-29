"""`ssisbliss format` against the real SSIS runtime and SQL Server: a seeded package's SQL is laid out, the
formatted file is loaded with SSIS's own object model (every statement must read back exactly as laid out),
deployed to a scratch folder and run in the catalog next to the original (both must succeed); and the
server's parser (SET PARSEONLY, what `ssisbliss lint --env` uses) accepts every seeded statement and
rejects broken SQL."""
import json
import subprocess
from pathlib import Path

from ssisbliss.ssis.catalog import SqlCatalog, connection_string
from ssisbliss.ssis.ispac import Ispac
from ssisbliss.ssis.lint import server_parse
from ssisbliss.ssis.sqlformat import format_sql
from ssisbliss.ssis.sqlsites import replace, sites

SERVER, SRC, F, P, PKG = "localhost,14331", "SILVER", "FMT", "SILVER", "Stage_Customer.dtsx"
WORK = Path(r"C:\lab\work\fmt")
WORK.mkdir(parents=True, exist_ok=True)
c = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app="ssisbliss-compare"))
project = c.get_project(SRC, P)
original = Ispac(project).package(PKG)
found = sites(original)
changes = {i: format_sql(s.sql)[0] for i, s in enumerate(found) if not s.dynamic and format_sql(s.sql)[0] != s.sql}
formatted = replace(original, changes, found)
(WORK / "original.dtsx").write_bytes(original)
(WORK / PKG).write_bytes(formatted)
expected = [changes.get(i, s.sql) for i, s in enumerate(found) if s.kind == "execute_sql"]

# 1. SSIS's object model reads the formatted package.
def object_model(path: Path) -> tuple[int | None, list[str] | None]:
    """(load errors, SQL statements) as SSIS's own object model reads the package."""
    ps = r"""
$dll = Get-ChildItem 'C:\Windows\Microsoft.NET\assembly\GAC_MSIL' -Recurse -Filter 'Microsoft.SqlServer.ManagedDTS.dll' | Where-Object FullName -like '*15.0.0.0*' | Select-Object -First 1
Add-Type -Path $dll.FullName
$app = New-Object Microsoft.SqlServer.Dts.Runtime.Application
$pkg = $app.LoadPackage('""" + str(path) + r"""', $null)
$out = @()
foreach ($e in $pkg.Executables) { if ($e -is [Microsoft.SqlServer.Dts.Runtime.TaskHost] -and $e.Properties.Contains('SqlStatementSource')) { $out += $e.Properties['SqlStatementSource'].GetValue($e) } }
"ERRORS:" + $pkg.Errors.Count
"SQL:" + (ConvertTo-Json @($out) -Compress)
"""
    res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    lines = res.stdout.splitlines()
    errors = next((int(l[7:]) for l in lines if l.startswith("ERRORS:")), None)
    sql = next((json.loads(l[4:]) for l in lines if l.startswith("SQL:")), None)
    return errors, [x.replace("\r\n", "\n") for x in sql] if sql is not None else None


errors_original, _ = object_model(WORK / "original.dtsx")
errors_formatted, loaded = object_model(WORK / PKG)
object_model_ok = loaded is not None and sorted(loaded) == sorted(expected) and errors_formatted == errors_original

# 2. Deploy the formatted package next to an untouched copy of the project and run both.
cur = c.conn.cursor()
cur.execute(f"IF NOT EXISTS (SELECT 1 FROM catalog.folders WHERE name = N'{F}') EXEC catalog.create_folder @folder_name = N'{F}'")
if c.project(F, P):
    cur.execute("EXEC catalog.delete_project @folder_name = ?, @project_name = ?", F, P)
c.deploy_project(F, P, project)
c.deploy_packages(F, P, [("Stage_Customer_original.dtsx", original), (PKG, formatted)])


FIRST_ERROR: dict[str, str] = {}


def run_package(name: str) -> int:
    row = cur.execute("""DECLARE @id bigint;
        EXEC catalog.create_execution @folder_name = ?, @project_name = ?, @package_name = ?, @execution_id = @id OUTPUT;
        EXEC catalog.set_execution_parameter_value @id, @object_type = 50, @parameter_name = N'SYNCHRONIZED', @parameter_value = 1;
        EXEC catalog.set_execution_parameter_value @id, @object_type = 20, @parameter_name = N'TargetDb', @parameter_value = N'SILVER';
        EXEC catalog.set_execution_parameter_value @id, @object_type = 20, @parameter_name = N'TargetServer', @parameter_value = N'localhost,14331';
        EXEC catalog.start_execution @id;
        SELECT status FROM catalog.executions WHERE execution_id = @id;""", F, P, name).fetchall()
    status = int(row[-1][0]) if row else -1
    if status != 7:
        msg = cur.execute("""SELECT TOP 1 m.message FROM catalog.event_messages m
                             WHERE m.operation_id = (SELECT MAX(execution_id) FROM catalog.executions WHERE package_name = ?)
                               AND m.message_type = 120 ORDER BY m.event_message_id""", name).fetchone()
        FIRST_ERROR[name] = (msg[0] if msg else "no error message")[:300]
    return status


status = {"original": run_package("Stage_Customer_original.dtsx"), "formatted": run_package(PKG)}
cur.execute("EXEC catalog.delete_project @folder_name = ?, @project_name = ?", F, P)

# 3. The server's parser.
seeded = [s.sql for n in Ispac(project).package_names for s in sites(Ispac(project).package(n)) if not s.dynamic]
rejected = [q for q in seeded if server_parse(c.conn, q)]
broken = server_parse(c.conn, "SELEC Id FROM dbo.Customer WHERE")
throw_ok = server_parse(c.conn, "IF NOT EXISTS (SELECT 1 FROM dbo.LoadLog WHERE Package = N'x') THROW 50001, N'no rows', 1;") is None
markers_ok = server_parse(c.conn, "UPDATE dbo.Batch SET Status = ? WHERE BatchId = ?") is None

result = {"statements": len(found), "laid_out": len(changes), "object_model_errors": [errors_original, errors_formatted],
          "object_model_reads_back_formatted": object_model_ok, "run_status": status, "run_errors": FIRST_ERROR,
          "seeded_statements": len(seeded), "server_rejected_seeded": rejected, "server_rejects_broken": broken,
          "throw_parses": throw_ok, "markers_parse": markers_ok}
result["ok"] = (len(changes) > 0 and object_model_ok and status["original"] == 7 and status["formatted"] == 7
                and not rejected and bool(broken) and throw_ok and markers_ok)
print(json.dumps(result, indent=1))
