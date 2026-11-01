# A project with real links for `ssisbliss deps`, built with the SSIS object model (ManagedDTS and the data flow
# API, as Visual Studio saves them) into C:\lab\work\deps\DEPS.ispac:
#   Extract  - Execute SQL: writes dbo.RawOrders from dbo.SourceOrders
#   Clean    - data flow: OLE DB source dbo.RawOrders -> OLE DB destination dbo.CleanOrders
#   Report   - Execute SQL: writes dbo.DailyTotals from dbo.CleanOrders
#   Master   - Execute Package tasks (project references): Extract, then Clean
#   Audit    - Execute SQL from an expression (cannot be followed), and one that reads dbo.DailyTotals
#   Other    - stands apart
$ErrorActionPreference = 'Stop'
$gac = 'C:\Windows\Microsoft.NET\assembly'
$dlls = foreach ($a in 'Microsoft.SqlServer.ManagedDTS', 'Microsoft.SqlServer.DTSPipelineWrap', 'Microsoft.SqlServer.DTSRuntimeWrap') {
    (Get-ChildItem $gac -Recurse -Filter "$a.dll" | Where-Object FullName -like '*15.0.0.0*' | Select-Object -First 1).FullName
}
$db = 'LABDEPS'
$conn = New-Object System.Data.SqlClient.SqlConnection 'Data Source=localhost,14331;Initial Catalog=master;Integrated Security=SSPI;'
$conn.Open()
$cmd = $conn.CreateCommand()
$cmd.CommandText = "IF DB_ID(N'$db') IS NULL CREATE DATABASE [$db]"
[void]$cmd.ExecuteNonQuery()
$cmd.CommandText = "USE [$db];
IF OBJECT_ID(N'dbo.SourceOrders') IS NULL CREATE TABLE dbo.SourceOrders (Id int NOT NULL, Amount decimal(18,2) NULL);
IF OBJECT_ID(N'dbo.RawOrders') IS NULL CREATE TABLE dbo.RawOrders (Id int NOT NULL, Amount decimal(18,2) NULL);
IF OBJECT_ID(N'dbo.CleanOrders') IS NULL CREATE TABLE dbo.CleanOrders (Id int NOT NULL, Amount decimal(18,2) NULL);
IF OBJECT_ID(N'dbo.DailyTotals') IS NULL CREATE TABLE dbo.DailyTotals (Total decimal(18,2) NULL);"
[void]$cmd.ExecuteNonQuery()
$conn.Close()

Add-Type -ReferencedAssemblies $dlls -TypeDefinition @'
using Microsoft.SqlServer.Dts.Runtime;
using Microsoft.SqlServer.Dts.Pipeline.Wrapper;

public static class DepsCorpus {
    const string Cs = "Data Source=localhost,14331;Initial Catalog=LABDEPS;Provider=MSOLEDBSQL;Integrated Security=SSPI;";

    static Package New(string name, out ConnectionManager cm) {
        var p = new Package { Name = name, ProtectionLevel = DTSProtectionLevel.DontSaveSensitive };
        cm = p.Connections.Add("OLEDB");
        cm.Name = "Target";
        cm.ConnectionString = Cs;
        return p;
    }

    static TaskHost Sql(Package p, ConnectionManager cm, string name, string sql) {
        var th = (TaskHost)p.Executables.Add("STOCK:SQLTask");
        th.Name = name;
        th.Properties["Connection"].SetValue(th, cm.ID);
        th.Properties["SqlStatementSource"].SetValue(th, sql);
        return th;
    }

    static TaskHost Run(Package p, string child) {
        var th = (TaskHost)p.Executables.Add("STOCK:ExecutePackageTask");
        th.Name = "Run " + child.Replace(".dtsx", "");
        th.Properties["UseProjectReference"].SetValue(th, true);
        th.Properties["PackageName"].SetValue(th, child);
        return th;
    }

    static IDTSComponentMetaData100 OleDb(MainPipe pipe, ConnectionManager cm, string cls, string name, string table) {
        var c = pipe.ComponentMetaDataCollection.New();
        c.ComponentClassID = cls;
        var w = c.Instantiate();
        w.ProvideComponentProperties();
        c.Name = name;
        c.RuntimeConnectionCollection[0].ConnectionManager = DtsConvert.GetExtendedInterface(cm);
        c.RuntimeConnectionCollection[0].ConnectionManagerID = cm.ID;
        w.SetComponentProperty("AccessMode", 0);
        w.SetComponentProperty("OpenRowset", table);
        w.AcquireConnections(null);
        w.ReinitializeMetaData();
        w.ReleaseConnections();
        return c;
    }

    public static void Build(string ispac) {
        var project = Project.CreateProject();
        project.Name = "DEPS";
        project.ProtectionLevel = DTSProtectionLevel.DontSaveSensitive;
        ConnectionManager cm;

        var extract = New("Extract", out cm);
        Sql(extract, cm, "Load raw", "INSERT INTO dbo.RawOrders (Id, Amount) SELECT Id, Amount FROM dbo.SourceOrders");
        project.PackageItems.Add(extract, "Extract.dtsx");

        var clean = New("Clean", out cm);
        var df = (TaskHost)clean.Executables.Add("STOCK:PipelineTask");
        df.Name = "Copy orders";
        var pipe = (MainPipe)df.InnerObject;
        var src = OleDb(pipe, cm, "Microsoft.OLEDBSource", "Raw orders", "[dbo].[RawOrders]");
        var dst = OleDb(pipe, cm, "Microsoft.OLEDBDestination", "Clean orders", "[dbo].[CleanOrders]");
        var path = pipe.PathCollection.New();
        path.AttachPathAndPropagateNotifications(src.OutputCollection[0], dst.InputCollection[0]);
        var input = dst.InputCollection[0];
        var vInput = input.GetVirtualInput();
        var design = dst.Instantiate();
        foreach (IDTSVirtualInputColumn100 v in vInput.VirtualInputColumnCollection) {
            var col = design.SetUsageType(input.ID, vInput, v.LineageID, DTSUsageType.UT_READONLY);
            design.MapInputColumn(input.ID, col.ID, input.ExternalMetadataColumnCollection[v.Name].ID);
        }
        project.PackageItems.Add(clean, "Clean.dtsx");

        var report = New("Report", out cm);
        Sql(report, cm, "Daily totals", "INSERT INTO dbo.DailyTotals (Total) SELECT SUM(Amount) FROM dbo.CleanOrders");
        project.PackageItems.Add(report, "Report.dtsx");

        var master = New("Master", out cm);
        var first = Run(master, "Extract.dtsx");
        var second = Run(master, "Clean.dtsx");
        master.PrecedenceConstraints.Add(first, second);
        project.PackageItems.Add(master, "Master.dtsx");

        var audit = New("Audit", out cm);
        audit.Variables.Add("TableName", false, "User", "dbo.DailyTotals");
        var dyn = Sql(audit, cm, "Count by name", "SELECT 1");
        dyn.SetExpression("SqlStatementSource", "\"SELECT COUNT(*) FROM \" + @[User::TableName]");
        Sql(audit, cm, "Check totals", "SELECT COUNT(*) FROM dbo.DailyTotals");
        project.PackageItems.Add(audit, "Audit.dtsx");

        var other = New("Other", out cm);
        Sql(other, cm, "Unrelated", "INSERT INTO dbo.OtherLog (Id) VALUES (0)");
        project.PackageItems.Add(other, "Other.dtsx");

        project.SaveTo(ispac);
        project.Dispose();
    }
}
'@
$dir = 'C:\lab\work\deps'
New-Item -ItemType Directory -Force $dir | Out-Null
Remove-Item -Force "$dir\DEPS.ispac" -ErrorAction SilentlyContinue
[DepsCorpus]::Build("$dir\DEPS.ispac")
Write-Output "DEPS CORPUS BUILT $((Get-Item "$dir\DEPS.ispac").Length) bytes"
