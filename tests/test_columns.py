"""Data flow columns against the server's tables (lint --env), and known-error explanations (executions)."""
from ssisbliss.ssis.columns import Col, check, endpoints, sql_column
from ssisbliss.ssis.dtsx import DTS
from ssisbliss.ssis.known_errors import explain


def _cols(cols, tag="externalMetadataColumn"):
    out = []
    for name, t, length, *ps in cols:
        prec = f' precision="{ps[0]}" scale="{ps[1]}"' if ps else ""
        out.append(f'<{tag} refId="x[{name}]" dataType="{t}" length="{length}" name="{name}"{prec} />')
    return "".join(out)


def package(source_cols, dest_cols, source_mode="0", source="[dbo].[Customer]", sql="", dest="[dbo].[DimCustomer]",
            dest_conn='connectionManagerID="{6A1E0000-0000-0000-0000-000000000001}:external" '
                      'connectionManagerRefId="Project.ConnectionManagers[Warehouse]"'):
    """A package with one data flow, laid out the way Visual Studio saves it (SSIS 2019)."""
    return f"""<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="{DTS}" DTS:refId="Package" DTS:ExecutableType="Microsoft.Package" DTS:ObjectName="Load_Customer">
  <DTS:ConnectionManagers>
    <DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[Source]" DTS:ObjectName="Source"
      DTS:DTSID="{{6A1E0000-0000-0000-0000-000000000002}}" DTS:CreationName="OLEDB">
      <DTS:ObjectData><DTS:ConnectionManager
        DTS:ConnectionString="Data Source=.;Initial Catalog=Sales;Provider=MSOLEDBSQL.1;Integrated Security=SSPI;" />
      </DTS:ObjectData>
    </DTS:ConnectionManager>
  </DTS:ConnectionManagers>
  <DTS:Executables>
    <DTS:Executable DTS:refId="Package\\Load customers" DTS:CreationName="Microsoft.Pipeline"
      DTS:ExecutableType="Microsoft.Pipeline" DTS:ObjectName="Load customers">
      <DTS:ObjectData><pipeline version="1"><components>
        <component refId="Package\\Load customers\\Customers" componentClassID="Microsoft.OLEDBSource" name="Customers">
          <properties>
            <property dataType="System.Int32" name="AccessMode">{source_mode}</property>
            <property dataType="System.String" name="OpenRowset">{source}</property>
            <property dataType="System.String" name="SqlCommand">{sql}</property>
          </properties>
          <connections><connection refId="c1" connectionManagerID="Package.ConnectionManagers[Source]"
            connectionManagerRefId="Package.ConnectionManagers[Source]" name="OleDbConnection" /></connections>
          <outputs>
            <output refId="o1" name="OLE DB Source Output"><externalMetadataColumns isUsed="True">{_cols(source_cols)}
            </externalMetadataColumns></output>
            <output refId="o2" isErrorOut="true" name="OLE DB Source Error Output" />
          </outputs>
        </component>
        <component refId="Package\\Load customers\\DimCustomer" componentClassID="Microsoft.OLEDBDestination" name="DimCustomer">
          <properties>
            <property dataType="System.Int32" name="AccessMode">3</property>
            <property dataType="System.String" name="OpenRowset">{dest}</property>
          </properties>
          <connections><connection refId="c2" {dest_conn} name="OleDbConnection" /></connections>
          <inputs><input refId="i1" name="OLE DB Destination Input"><externalMetadataColumns isUsed="True">{_cols(dest_cols)}
          </externalMetadataColumns></input></inputs>
        </component>
      </components></pipeline></DTS:ObjectData>
    </DTS:Executable>
  </DTS:Executables>
</DTS:Executable>""".encode()


WAREHOUSE = (f'<?xml version="1.0"?><DTS:ConnectionManager xmlns:DTS="{DTS}" DTS:ObjectName="Warehouse" '
             'DTS:DTSID="{6A1E0000-0000-0000-0000-000000000001}" DTS:CreationName="OLEDB"><DTS:ObjectData>'
             '<DTS:ConnectionManager DTS:ConnectionString="Data Source=.;Initial Catalog=DW;Provider=MSOLEDBSQL.1;" />'
             '</DTS:ObjectData></DTS:ConnectionManager>')

SRC = [("CustomerId", "i4", 0), ("Name", "wstr", 50), ("Balance", "numeric", 0, 18, 2)]


class Schema:
    """Tables and query results the way ServerSchema reports them, from sys.columns-style values."""

    def __init__(self, tables, queries=None):
        self.tables, self.queries = tables, queries or {}

    def has_database(self, name):
        return name.lower() in {k[0].lower() for k in self.tables}

    def table(self, database, table):
        return self.tables.get((database, table))

    def query(self, database, sql):
        return self.queries.get(sql, "Invalid object name 'dbo.Nope'.")


def server(dest_extra=(), name_bytes=100, dim_name_bytes=100):
    return Schema({
        ("Sales", "[dbo].[Customer]"): [sql_column("CustomerId", "int", 4, 10, 0),
                                       sql_column("Name", "nvarchar", name_bytes, 0, 0),
                                       sql_column("Balance", "decimal", 9, 18, 2)],
        ("DW", "[dbo].[DimCustomer]"): [sql_column("CustomerKey", "int", 4, 10, 0),   # identity: not required
                                        sql_column("CustomerId", "int", 4, 10, 0, required=True),
                                        sql_column("Name", "nvarchar", dim_name_bytes, 0, 0),
                                        sql_column("Balance", "decimal", 9, 18, 2), *dest_extra]})


def run(tmp_path, data, schema):
    (tmp_path / "Warehouse.conmgr").write_text(WAREHOUSE)
    return check("Load_Customer.dtsx", data, schema, "dev", tmp_path)


def test_endpoints_read_tables_columns_and_project_connections(tmp_path):
    (tmp_path / "Warehouse.conmgr").write_text(WAREHOUSE)
    src, dest = endpoints(package(SRC, SRC[:2]), tmp_path)
    assert (src.role, src.connection, src.database, src.table) == ("source", "Source", "Sales", "[dbo].[Customer]")
    assert (dest.role, dest.connection, dest.database, dest.table) == ("destination", "Warehouse", "DW", "[dbo].[DimCustomer]")
    assert [c.show() for c in src.columns] == ["i4", "wstr(50)", "numeric(18,2)"]
    assert src.task == "Load customers" and src.path == "Package\\Load customers"


def test_package_in_step_with_the_tables_has_no_findings(tmp_path):
    assert run(tmp_path, package(SRC, SRC), server()) == []


def test_a_dropped_column_would_fail_with_needs_new_metadata(tmp_path):
    schema = server()
    schema.tables[("Sales", "[dbo].[Customer]")].pop(2)   # Balance dropped from the source table
    [f] = run(tmp_path, package(SRC, SRC), schema)
    assert f.rule == "SSB402" and f.severity == "error" and "Balance" in f.message and "VS_NEEDSNEWMETADATA" in f.message
    assert f.task == "Load customers" and "Sales.[dbo].[Customer]" in f.message


def test_a_changed_type_is_an_error(tmp_path):
    schema = server()
    schema.tables[("Sales", "[dbo].[Customer]")][0] = sql_column("CustomerId", "bigint", 8, 19, 0)
    [f] = run(tmp_path, package(SRC, SRC), schema)
    assert f.rule == "SSB403" and "i8 on the server but i4" in f.message


def test_lengths_that_truncate_are_errors_and_the_others_warnings(tmp_path):
    # The source table's Name grew to 100 characters: values over 50 no longer fit the data flow.
    [f] = run(tmp_path, package(SRC, SRC), server(name_bytes=200))
    assert f.rule == "SSB404" and f.severity == "error" and "longer than 50" in f.message and "Customers" in f.message
    # The destination's column shrank below the data flow's: inserts of long names fail.
    [f] = run(tmp_path, package(SRC, SRC), server(dim_name_bytes=40))
    assert f.severity == "error" and "longer than 20" in f.message and "DimCustomer" in f.message
    # The destination grew: nothing fails, the metadata is only out of date.
    [f] = run(tmp_path, package(SRC, SRC), server(dim_name_bytes=200))
    assert f.severity == "warn"


def test_a_new_not_null_column_in_the_destination_fails_every_insert(tmp_path):
    [f] = run(tmp_path, package(SRC, SRC), server(dest_extra=[sql_column("Region", "varchar", 10, 0, 0, required=True)]))
    assert f.rule == "SSB405" and "Region" in f.message
    assert run(tmp_path, package(SRC, SRC), server(dest_extra=[sql_column("Region", "varchar", 10, 0, 0)])) == []


def test_missing_table_and_what_cannot_be_checked(tmp_path):
    findings = run(tmp_path, package(SRC, SRC, dest="[dbo].[Nope]"), server())
    assert [f.rule for f in findings] == ["SSB401"]
    findings = run(tmp_path, package(SRC, SRC, source_mode="1"), server())
    assert [(f.rule, f.severity) for f in findings] == [("SSB409", "info")] and "variable" in findings[0].message
    other = Schema({("Elsewhere", "x"): []})
    assert {f.rule for f in run(tmp_path, package(SRC, SRC), other)} == {"SSB409"}


def test_query_sources_are_compared_with_the_querys_result(tmp_path):
    sql = "SELECT CustomerId, Name, Balance FROM dbo.Customer WHERE Region = ?"
    schema = server()
    schema.queries[sql] = [sql_column("CustomerId", "int", 4, 10, 0), sql_column("Name", "nvarchar", 100, 0, 0)]
    [f] = run(tmp_path, package(SRC, SRC, source_mode="2", sql=sql), schema)
    assert f.rule == "SSB402" and "Balance" in f.message and "its query in Sales" in f.message
    findings = run(tmp_path, package(SRC, SRC, source_mode="2", sql="SELECT * FROM dbo.Nope"), schema)
    assert findings[0].rule == "SSB401" and "Invalid object name" in findings[0].message
    schema.queries["SELECT 1"] = "Incorrect syntax near 'x'."
    findings = run(tmp_path, package(SRC, SRC, source_mode="2", sql="SELECT 1"), schema)
    assert findings[0].rule == "SSB409" and "Incorrect syntax" in findings[0].message


def test_the_servers_real_message_not_the_generic_one():
    from ssisbliss.ssis.columns import server_message
    error = ("('42000', \"[42000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Invalid column name 'Balance'. "
             "(207) (SQLExecDirectW); [42000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]The batch could "
             "not be analyzed because of compile errors. (11501)\")")
    assert server_message(error) == "Invalid column name 'Balance'."


def test_server_types_map_to_data_flow_types():
    assert sql_column("x", "nvarchar", -1, 0, 0).type == "ntext"
    assert sql_column("x", "varchar", 30, 0, 0).show() == "str(30)"
    assert sql_column("x", "datetime2", 8, 27, 7) == Col("x", "dbTimeStamp2", 0, 0, 7)


# ------------------------------------------------------------- known errors --

def test_an_oledb_wrapper_is_unwrapped_and_explained():
    messages = ['SSIS Error Code DTS_E_OLEDBERROR.  An OLE DB error has occurred. Error code: 0x80004005.\n'
                'An OLE DB record is available.  Source: "Microsoft OLE DB Driver for SQL Server"  Hresult: 0x80004005  '
                'Description: "Login failed for user \'LAB\\svc_agent\'.".']
    found = {e["error"]: e for e in explain(messages, "LAB\\svc_agent")}
    assert found["oledb-wrapper"]["real_error"] == ["Login failed for user 'LAB\\svc_agent'."]
    assert found["login-failed"]["run_as"] == "LAB\\svc_agent" and "Visual Studio" in found["login-failed"]["cause"]


def test_common_failures_get_a_cause_and_a_fix():
    cases = {
        '"OLE DB Destination" failed validation and returned validation status "VS_NEEDSNEWMETADATA".': "needs-new-metadata",
        'The requested OLE DB provider Microsoft.ACE.OLEDB.16.0 is not registered.': "provider-not-registered",
        'Data conversion failed. The data conversion for column "Name" returned status value 4 and status text '
        '"Text was truncated or one or more characters had no match in the target code page.".': "truncation",
        'Transaction (Process ID 61) was deadlocked on lock resources with another process.': "deadlock",
        'Could not find a part of the path \'Z:\\inbox\\prices.csv\'.': "file-missing",
        "The package is configured to use an environment variable but no environment has been selected.": "no-environment",
        "Invalid object name 'stage.Customer'.": "invalid-object",
    }
    for message, expected in cases.items():
        assert expected in [e["error"] for e in explain([message])], message
    assert explain(["Process exited with code 2 (lsn 1205, 18456 rows)"]) == []   # no false alarms on numbers
