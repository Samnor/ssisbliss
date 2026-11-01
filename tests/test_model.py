"""The package model: one read of a package that outline, lint, the design rules, the column checks and deps share."""
import sys
from pathlib import Path

import pytest

CLIMB = Path(__file__).resolve().parents[1] / "climb"
if not CLIMB.is_dir():   # the source archive ships the package's tests, not the climb
    pytest.skip("climb/ is only in the repository", allow_module_level=True)
sys.path.insert(0, str(CLIMB))
import depscases  # noqa: E402
from world import task_package  # noqa: E402

from ssisbliss.ssis.model import Package, ProjectInfo, evaluate  # noqa: E402
from ssisbliss.ssis.outline import outline  # noqa: E402

D = "www.microsoft.com/SqlServer/Dts"


def project(folder: Path, layer: str = "SILVER") -> ProjectInfo:
    (folder / "Project.params").write_text(depscases._params(depscases.LAYERS[layer]))
    return ProjectInfo.from_folder(folder, layer)


def test_connections_know_their_database_and_where_it_comes_from(tmp_path):
    p = Package.parse(depscases.PACKAGES["SILVER"]["Load_Customer.dtsx"], project(tmp_path))
    source = p.connection("Package.ConnectionManagers[Source]")
    assert source is p.connection("{00000000-0000-0000-0000-000000000002}") is p.connection("Source")
    assert (source.database, source.stored_database, source.type) == ("dw_local", "DW_LOCAL", "OLEDB")
    assert source.assumed == "$Project::SourceDb in SILVER = DW_LOCAL"
    # without the project's values the expression cannot be evaluated: not known, rather than the stored string
    assert Package.parse(depscases.PACKAGES["SILVER"]["Load_Customer.dtsx"]).connection("Source").database is None
    # the deployed project's values are not design values
    deployed = ProjectInfo("SILVER", {"sourcedb": "BRONZE_PROD", "targetdb": "SILVER_PROD"})
    target = Package.parse(depscases.PACKAGES["SILVER"]["Load_Customer.dtsx"], deployed).connection("Target")
    assert (target.database, target.assumed) == ("silver_prod", None)


def test_a_catalog_value_for_a_project_connection_manager_wins():
    conmgr = (f'<DTS:ConnectionManager xmlns:DTS="{D}" DTS:ObjectName="Warehouse" DTS:DTSID="{{AAAA}}" '
              'DTS:CreationName="OLEDB"><DTS:ObjectData><DTS:ConnectionManager DTS:ConnectionString="Data Source=.;'
              'Initial Catalog=DW_DESIGN;" /></DTS:ObjectData></DTS:ConnectionManager>').encode()
    data = task_package("Load.dtsx", sql=["INSERT INTO dbo.t (a) SELECT a FROM dbo.s"])
    files = Package.parse(data, ProjectInfo("P", {}, [conmgr], design=True)).connection("Project.ConnectionManagers[Warehouse]")
    assert files.database == "dw_design" and files.assumed == "project connection manager Warehouse in P = DW_DESIGN"
    catalog = ProjectInfo("P", {"cm.warehouse.initialcatalog": "DW_PROD"}, [conmgr])
    assert Package.parse(data, catalog).connection("Warehouse").database == "dw_prod"


def test_tasks_in_run_order_and_every_task_with_event_handlers():
    data = task_package("Main.dtsx", sql=["SELECT 1", "SELECT 2"], calls=["Child.dtsx"]).decode()
    handler = ('<DTS:EventHandlers><DTS:EventHandler DTS:refId="Package.EventHandlers[OnError]" DTS:EventName="OnError">'
               '<DTS:Executables><DTS:Executable DTS:refId="Package.EventHandlers[OnError]\\Log" '
               'DTS:ExecutableType="Microsoft.ExecuteSQLTask" DTS:ObjectName="Log"><DTS:ObjectData>'
               '<SQLTask:SqlTaskData xmlns:SQLTask="www.microsoft.com/sqlserver/dts/tasks/sqltask" '
               'SQLTask:SqlStatementSource="INSERT INTO dbo.ErrorLog (a) VALUES (1)" /></DTS:ObjectData>'
               '</DTS:Executable></DTS:Executables></DTS:EventHandler></DTS:EventHandlers>')
    data = data.replace("</DTS:Executables>\n</DTS:Executable>", "</DTS:Executables>" + handler + "\n</DTS:Executable>")
    p = Package.parse(data.encode())
    assert [t.name for t in p.walk()] == ["SQL 1", "SQL 2", "Run Child.dtsx"]
    assert [t.name for t in p.every_task] == ["SQL 1", "SQL 2", "Run Child.dtsx", "Log"]
    log = p.every_task[-1]
    assert log.is_sql and log.sql.startswith("INSERT INTO dbo.ErrorLog") and log.sql_source == "DirectInput"


def test_data_flow_components_find_their_connection(tmp_path):
    p = Package.parse(depscases.PACKAGES["GOLD"]["Dim_Customer.dtsx"], project(tmp_path, "GOLD"))
    flow = next(t for t in p.every_task if t.is_dataflow)
    read, write = flow.components
    assert (read.name, read.cls, read.prop("OpenRowset"), read.connection_name) == (
        "Read", "Microsoft.OLEDBSource", "[dbo].[Customer]", "Source")
    assert write.connection.name == "Target" and write.connection.assumed == "$Project::TargetDb in GOLD = DW_LOCAL"


def test_outline_shows_each_connections_database(tmp_path):
    o = outline(depscases.PACKAGES["SILVER"]["Load_Customer.dtsx"], project(tmp_path))
    assert [(c["name"], c["database"]) for c in o["connections"]] == [("Source", "dw_local"), ("Target", "dw_local")]
    assert o["connections"][0]["database_from"] == ("$Project::SourceDb in SILVER = DW_LOCAL in your files; the catalog "
                                                     "may set another value")
    assert "database" not in str(outline(task_package("Plain.dtsx", sql=["SELECT 1"])))


def test_expressions_evaluate_only_what_they_can():
    vals = {"$project::db": "SALES", "user::server": "srv"}
    assert evaluate('"Data Source=" + @[User::Server] + ";Initial Catalog=" + @[$Project::Db]', vals) == \
        "Data Source=srv;Initial Catalog=SALES"
    assert evaluate('@[$Project::Missing]', vals) is None
    assert evaluate('UPPER(@[$Project::Db])', vals) is None       # a function: not followed


def test_a_failed_data_flow_says_which_database_its_component_used():
    """executions: the failing component's own connection, with the values this run had."""
    from ssisbliss.ssis.executions import report
    from ssisbliss.ssis.fake import FakeCatalog, ispac
    cat = FakeCatalog()
    cat.add_project("SILVER", "SILVER", ispac("SILVER", {"Load_Customer.dtsx": depscases.PACKAGES["SILVER"]["Load_Customer.dtsx"]},
                                              parameters=["SourceDb", "TargetDb"]))
    cat.execution_rows = [{"execution_id": 9, "folder_name": "SILVER", "project_name": "SILVER", "status": 4,
                           "package_name": "Load_Customer.dtsx", "environment_folder_name": None, "environment_name": None,
                           "caller_name": "agent", "executed_as_name": "agent", "start_time": "2099-01-01T01:00:00",
                           "end_time": "2099-01-01T01:00:01", "duration_s": 1, "server_name": "S"}]
    cat.event_rows = {9: [{"event_message_id": 1, "message_time": "", "message_type": 120, "message_source_type": 40,
                           "execution_path": "", "message_source_name": "Copy", "package_name": "Load_Customer.dtsx",
                           "package_path": "\\Package\\Copy", "subcomponent_name": "Read",
                           "message": "Copy:Error: Cannot open database requested by the login."}]}
    cat.parameter_rows = {9: [{"object_type": 20, "parameter_name": n, "parameter_value": v, "sensitive": False,
                               "runtime_override": False} for n, v in (("SourceDb", "BRONZE_PROD"), ("TargetDb", "SILVER_PROD"))]}
    [e] = report(cat, cat.executions(execution_id=9))
    [t] = e["failed_tasks"]
    assert (t["component"], t["database"], t["database_connection"]) == ("Read", "bronze_prod", "Source")


def test_package_parameters_set_from_outside_win():
    data = depscases.layer_package("P.dtsx", ["SELECT 1"]).replace(b"@[$Project::TargetDb]", b"@[$Package::Db]")
    assert Package.parse(data, ProjectInfo(package_values={"db": "SALES"})).connection("Target").database == "sales"
