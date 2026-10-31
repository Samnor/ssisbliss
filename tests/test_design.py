"""Package design rules (design.py): row-by-row data flows, Excel drivers, temp tables, written-in servers."""
from ssisbliss.ssis.design import design_findings, excel_providers, missing_providers
from ssisbliss.ssis.dtsx import DTS
from ssisbliss.ssis.lint import lint_package

SQLTASK = "www.microsoft.com/sqlserver/dts/tasks/sqltask"


def cm(name, string, retain=False, expression=None, kind="OLEDB"):
    keep = ' DTS:Retain="True"' if retain else ""
    expr = f'<DTS:PropertyExpression DTS:Name="{expression}">@[$Project::Server]</DTS:PropertyExpression>' if expression else ""
    return (f'<DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[{name}]" DTS:ObjectName="{name}" '
            f'DTS:DTSID="{{{name}}}" DTS:CreationName="{kind}">{expr}<DTS:ObjectData><DTS:ConnectionManager '
            f'DTS:ConnectionString="{string}"{keep} /></DTS:ObjectData>'
            '</DTS:ConnectionManager>')


def component(cls, name, **props):
    p = "".join(f'<property name="{k}">{v}</property>' for k, v in props.items())
    return f'<component refId="{name}" componentClassID="{cls}" name="{name}"><properties>{p}</properties></component>'


def package(conns="", flow="", sql=None, sql_conn="Db"):
    sql_task = (f'<DTS:Executable DTS:refId="Package\\Make temp" DTS:ExecutableType="Microsoft.ExecuteSQLTask" '
                f'DTS:ObjectName="Make temp"><DTS:ObjectData><SQLTask:SqlTaskData xmlns:SQLTask="{SQLTASK}" '
                f'SQLTask:Connection="{{{sql_conn}}}" SQLTask:SqlStatementSource="{sql}" /></DTS:ObjectData>'
                '</DTS:Executable>') if sql else ""
    flow_task = (f'<DTS:Executable DTS:refId="Package\\Load" DTS:ExecutableType="Microsoft.Pipeline" DTS:ObjectName="Load">'
                 f'<DTS:ObjectData><pipeline version="1"><components>{flow}</components></pipeline></DTS:ObjectData>'
                 '</DTS:Executable>') if flow else ""
    return (f'<?xml version="1.0"?><DTS:Executable xmlns:DTS="{DTS}" DTS:refId="Package" '
            f'DTS:ExecutableType="Microsoft.Package" DTS:ObjectName="P"><DTS:ConnectionManagers>{conns}'
            f'</DTS:ConnectionManagers><DTS:Executables>{sql_task}{flow_task}</DTS:Executables></DTS:Executable>').encode()


LOCAL = "Data Source=localhost;Initial Catalog=Stage;Provider=MSOLEDBSQL;Integrated Security=SSPI;"


def rules(data, project_dir=None):
    return sorted(f.rule for f in design_findings("P.dtsx", data, project_dir))


def test_row_by_row_data_flows():
    flow = (component("Microsoft.OLEDBCommand", "Update rows")
            + component("Microsoft.SCD", "Dim history")
            + component("Microsoft.OLEDBDestination", "Slow insert", AccessMode="0")
            + component("Microsoft.OLEDBDestination", "Fast insert", AccessMode="3")
            + component("Microsoft.ADONETDestination", "Ado insert", UseBulkInsertWhenPossible="false")
            + component("Microsoft.Lookup", "No cache", CacheType="2")
            + component("Microsoft.Lookup", "Full cache", CacheType="0")
            + component("Microsoft.Sort", "Sort rows"))
    found = design_findings("P.dtsx", package(cm("Db", LOCAL), flow))
    by = {f.task: f.rule for f in found}
    assert by == {"Load\\Update rows": "SSB501", "Load\\Dim history": "SSB501", "Load\\Slow insert": "SSB502",
                  "Load\\Ado insert": "SSB502", "Load\\No cache": "SSB503", "Load\\Sort rows": "SSB504"}


def test_excel_through_jet_and_ace():
    jet = cm("Old xls", "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=C:\\in\\a.xls;Extended Properties=Excel 8.0;",
             kind="EXCEL")
    ace = cm("New xlsx", "Provider=Microsoft.ACE.OLEDB.16.0;Data Source=C:\\in\\b.xlsx;Extended Properties=Excel 12.0;",
             kind="EXCEL")
    data = package(jet + ace)
    found = {f.task: f for f in design_findings("P.dtsx", data)}
    assert found["Old xls"].severity == "warn" and "32-bit" in found["Old xls"].message
    assert "Microsoft.ACE.OLEDB.16.0" in found["New xlsx"].message
    assert excel_providers(data) == {"Microsoft.Jet.OLEDB.4.0": "Old xls", "Microsoft.ACE.OLEDB.16.0": "New xlsx"}
    missing = missing_providers("P.dtsx", data, {"MSOLEDBSQL", "microsoft.ace.oledb.16.0"}, "prod")
    assert [(f.rule, f.task) for f in missing] == [("SSB511", "Old xls")]
    assert "not installed on prod" in missing[0].message


def test_temp_table_needs_the_same_session():
    sql = "SELECT * INTO #work FROM dbo.Customer;"
    assert rules(package(cm("Db", LOCAL), sql=sql)) == ["SSB520"]
    assert rules(package(cm("Db", LOCAL, retain=True), sql=sql)) == []
    assert rules(package(cm("Db", LOCAL), sql="CREATE TABLE dbo.Work (Id int);")) == []


def test_project_connection_managers_count(tmp_path):
    (tmp_path / "Db.conmgr").write_bytes(
        f'<?xml version="1.0"?><DTS:ConnectionManager xmlns:DTS="{DTS}" DTS:ObjectName="Db" DTS:DTSID="{{Db}}" '
        f'DTS:CreationName="OLEDB"><DTS:ObjectData><DTS:ConnectionManager DTS:ConnectionString="{LOCAL}" '
        'DTS:Retain="True" /></DTS:ObjectData></DTS:ConnectionManager>'.encode())
    assert rules(package(sql="CREATE TABLE #t (Id int);"), tmp_path) == []
    (tmp_path / "Db.conmgr").write_bytes((tmp_path / "Db.conmgr").read_bytes().replace(b' DTS:Retain="True"', b""))
    assert rules(package(sql="CREATE TABLE #t (Id int);"), tmp_path) == ["SSB520"]


def test_a_server_written_in_the_package():
    prod = "Data Source=SQLPROD01\\DW;Initial Catalog=Stage;Provider=MSOLEDBSQL;Integrated Security=SSPI;"
    assert rules(package(cm("Db", prod))) == ["SSB521"]
    assert rules(package(cm("Db", prod, expression="ConnectionString"))) == []
    assert rules(package(cm("Db", LOCAL))) == []


def test_design_rules_run_in_lint():
    found = lint_package("P.dtsx", package(cm("Db", LOCAL), component("Microsoft.OLEDBCommand", "Row by row")))
    assert [f.rule for f in found] == ["SSB501"]
