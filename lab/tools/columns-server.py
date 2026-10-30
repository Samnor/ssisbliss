"""`ssisbliss lint --env` column checks against a real SQL Server: a scratch database gets the tables a data flow
reads and writes, then changes that break packages in production (a wider source column, a new NOT NULL
destination column, a dropped column) must each be found, read through sys.columns and
sp_describe_first_result_set (a query source with a ? marker)."""
import json

from ssisbliss.ssis.catalog import SqlCatalog, connection_string
from ssisbliss.ssis.columns import ServerSchema, check
from ssisbliss.ssis.dtsx import DTS

SERVER, DB = "localhost,14331", "LABCOLS"
c = SqlCatalog(connection_string(SERVER, trust_server_certificate=True, app="ssisbliss-compare"))
cur = c.conn.cursor()
cur.execute(f"IF DB_ID(N'{DB}') IS NULL CREATE DATABASE [{DB}]")


def sql(statement: str) -> None:
    c.conn.cursor().execute(f"USE [{DB}]; {statement}")


sql("DROP TABLE IF EXISTS dbo.Customer; DROP TABLE IF EXISTS dbo.DimCustomer;"
    "CREATE TABLE dbo.Customer (CustomerId int NOT NULL, Name nvarchar(50) NULL, Balance decimal(18,2) NULL);"
    "CREATE TABLE dbo.DimCustomer (CustomerKey int IDENTITY PRIMARY KEY, CustomerId int NOT NULL, "
    "Name nvarchar(50) NULL, Balance decimal(18,2) NULL, Loaded datetime2 NOT NULL DEFAULT SYSDATETIME());")

COLS = [("CustomerId", "i4", 0, None), ("Name", "wstr", 50, None), ("Balance", "numeric", 0, (18, 2))]


def meta(cols):
    return "".join(f'<externalMetadataColumn refId="x[{n}]" dataType="{t}" length="{l}" name="{n}"'
                   + (f' precision="{p[0]}" scale="{p[1]}"' if p else "") + " />" for n, t, l, p in cols)


def package(mode: str, query: str = "") -> bytes:
    conn = (f'<connections><connection refId="c" connectionManagerID="Package.ConnectionManagers[Db]" '
            f'connectionManagerRefId="Package.ConnectionManagers[Db]" name="OleDbConnection" /></connections>')
    return f"""<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="{DTS}" DTS:refId="Package" DTS:ExecutableType="Microsoft.Package" DTS:ObjectName="Cols">
  <DTS:ConnectionManagers><DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[Db]" DTS:ObjectName="Db"
    DTS:DTSID="{{11111111-0000-0000-0000-000000000001}}" DTS:CreationName="OLEDB"><DTS:ObjectData><DTS:ConnectionManager
    DTS:ConnectionString="Data Source=localhost,14331;Initial Catalog={DB};Provider=MSOLEDBSQL.1;Integrated Security=SSPI;" />
  </DTS:ObjectData></DTS:ConnectionManager></DTS:ConnectionManagers>
  <DTS:Executables><DTS:Executable DTS:refId="Package\\Load" DTS:ExecutableType="Microsoft.Pipeline" DTS:ObjectName="Load">
    <DTS:ObjectData><pipeline version="1"><components>
      <component refId="s" componentClassID="Microsoft.OLEDBSource" name="Customers"><properties>
        <property name="AccessMode">{mode}</property><property name="OpenRowset">[dbo].[Customer]</property>
        <property name="SqlCommand">{query}</property></properties>{conn}
        <outputs><output refId="o" name="Output"><externalMetadataColumns isUsed="True">{meta(COLS)}</externalMetadataColumns></output></outputs>
      </component>
      <component refId="d" componentClassID="Microsoft.OLEDBDestination" name="DimCustomer"><properties>
        <property name="AccessMode">3</property><property name="OpenRowset">[dbo].[DimCustomer]</property></properties>{conn}
        <inputs><input refId="i" name="Input"><externalMetadataColumns isUsed="True">{meta(COLS)}</externalMetadataColumns></input></inputs>
      </component>
    </components></pipeline></DTS:ObjectData></DTS:Executable></DTS:Executables>
</DTS:Executable>""".encode()


def findings(data: bytes) -> list[dict]:
    return [{"rule": f.rule, "severity": f.severity, "message": f.message}
            for f in check("Cols.dtsx", data, ServerSchema(c.conn), "lab")]


table_source = package("0")
query_source = package("2", "SELECT CustomerId, Name, Balance FROM dbo.Customer WHERE CustomerId &gt; ?")
result: dict = {"in_step": findings(table_source), "in_step_query": findings(query_source)}

sql("ALTER TABLE dbo.Customer ALTER COLUMN Name nvarchar(100) NULL;"
    "ALTER TABLE dbo.DimCustomer ADD Region varchar(10) NOT NULL;"
    "ALTER TABLE dbo.Customer DROP COLUMN Balance;")
result["changed"] = findings(table_source)
result["changed_query"] = findings(query_source)

rules = lambda key: sorted(f["rule"] for f in result[key] if f["severity"] != "info")  # noqa: E731
result["ok"] = (result["in_step"] == [] and result["in_step_query"] == []
                and rules("changed") == ["SSB402", "SSB404", "SSB405"]
                # the query names Balance, which is gone: the server cannot describe it, said as not checked
                and any(f["rule"] == "SSB409" and "Balance" in f["message"] for f in result["changed_query"]))
cur.execute(f"USE master; ALTER DATABASE [{DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{DB}]")
print(json.dumps(result, indent=1))
