# A medallion estate for `ssisbliss deps`: three projects, one database per layer (LABBRONZE, LABSILVER, LABGOLD),
# each with a dbo.Customer, built with the SSIS object model into C:\lab\work\medallion\<LAYER>.ispac. Every
# connection's database comes from a project parameter through an expression; the parameters' design value is
# DESIGN, and deps-medallion.py binds them to environment variables after the deploy.
#   BRONZE  Load_Customer   SQL: dbo.CustomerFeed -> dbo.Customer
#           Check_Customer  SQL: reads dbo.Customer (bronze's own)
#   SILVER  Load_Customer   data flow: Source (SourceDb = bronze) dbo.Customer -> Target (TargetDb) dbo.Customer
#           Load_Order      SQL: reads dbo.Customer (silver's)
#   GOLD    Dim_Customer    SQL: reads LABSILVER.dbo.Customer (three-part name)
#           Load_Customer   SQL: writes gold's own dbo.Customer from dbo.DimCustomer
$ErrorActionPreference = 'Stop'
$gac = 'C:\Windows\Microsoft.NET\assembly'
$dlls = foreach ($a in 'Microsoft.SqlServer.ManagedDTS', 'Microsoft.SqlServer.DTSPipelineWrap', 'Microsoft.SqlServer.DTSRuntimeWrap') {
    (Get-ChildItem $gac -Recurse -Filter "$a.dll" | Where-Object FullName -like '*15.0.0.0*' | Select-Object -First 1).FullName
}
$conn = New-Object System.Data.SqlClient.SqlConnection 'Data Source=localhost,14331;Initial Catalog=master;Integrated Security=SSPI;'
$conn.Open()
foreach ($db in 'LABBRONZE', 'LABSILVER', 'LABGOLD') {
    $cmd = $conn.CreateCommand()
    $cmd.CommandText = "IF DB_ID(N'$db') IS NULL CREATE DATABASE [$db]"
    [void]$cmd.ExecuteNonQuery()
    $cmd.CommandText = "USE [$db];
    IF OBJECT_ID(N'dbo.Customer') IS NULL CREATE TABLE dbo.Customer (Id int NOT NULL, Name nvarchar(50) NULL);
    IF OBJECT_ID(N'dbo.CustomerFeed') IS NULL CREATE TABLE dbo.CustomerFeed (Id int NOT NULL, Name nvarchar(50) NULL);"
    [void]$cmd.ExecuteNonQuery()
}
$conn.Close()

Add-Type -ReferencedAssemblies $dlls -TypeDefinition @'
using Microsoft.SqlServer.Dts.Runtime;
using Microsoft.SqlServer.Dts.Pipeline.Wrapper;

public static class Medallion {
    static ConnectionManager Cm(Package p, string name, string designDb, string parameter) {
        var cm = p.Connections.Add("OLEDB");
        cm.Name = name;
        cm.ConnectionString = "Data Source=localhost,14331;Initial Catalog=" + designDb + ";Provider=MSOLEDBSQL;Integrated Security=SSPI;";
        cm.SetExpression("ConnectionString",
            "\"Data Source=localhost,14331;Initial Catalog=\" + @[$Project::" + parameter + "] + \";Provider=MSOLEDBSQL;Integrated Security=SSPI;\"");
        return cm;
    }

    static Package New(string name) {
        return new Package { Name = name, ProtectionLevel = DTSProtectionLevel.DontSaveSensitive };
    }

    static void Sql(Package p, ConnectionManager cm, string name, string sql) {
        var th = (TaskHost)p.Executables.Add("STOCK:SQLTask");
        th.Name = name;
        th.Properties["Connection"].SetValue(th, cm.ID);
        th.Properties["SqlStatementSource"].SetValue(th, sql);
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

    static Project NewProject(string name, params string[] parameters) {
        var project = Project.CreateProject();
        project.Name = name;
        project.ProtectionLevel = DTSProtectionLevel.DontSaveSensitive;
        foreach (var p in parameters) project.Parameters.Add(p, System.TypeCode.String).Value = "DESIGN";
        return project;
    }

    public static void Build(string dir) {
        var bronze = NewProject("BRONZE", "TargetDb");
        var load = New("Load_Customer");
        Sql(load, Cm(load, "Target", "LABBRONZE", "TargetDb"), "Load customers",
            "INSERT INTO dbo.Customer (Id, Name) SELECT Id, Name FROM dbo.CustomerFeed");
        bronze.PackageItems.Add(load, "Load_Customer.dtsx");
        var check = New("Check_Customer");
        Sql(check, Cm(check, "Target", "LABBRONZE", "TargetDb"), "Count customers",
            "INSERT INTO dbo.Checks (Id) SELECT Id FROM dbo.Customer");
        bronze.PackageItems.Add(check, "Check_Customer.dtsx");
        bronze.SaveTo(dir + "\\BRONZE.ispac");
        bronze.Dispose();

        var silver = NewProject("SILVER", "TargetDb", "SourceDb");
        var copy = New("Load_Customer");
        var source = Cm(copy, "Source", "LABBRONZE", "SourceDb");
        var target = Cm(copy, "Target", "LABSILVER", "TargetDb");
        var df = (TaskHost)copy.Executables.Add("STOCK:PipelineTask");
        df.Name = "Copy customers";
        var pipe = (MainPipe)df.InnerObject;
        var src = OleDb(pipe, source, "Microsoft.OLEDBSource", "Bronze customers", "[dbo].[Customer]");
        var dst = OleDb(pipe, target, "Microsoft.OLEDBDestination", "Silver customers", "[dbo].[Customer]");
        pipe.PathCollection.New().AttachPathAndPropagateNotifications(src.OutputCollection[0], dst.InputCollection[0]);
        var input = dst.InputCollection[0];
        var vInput = input.GetVirtualInput();
        var design = dst.Instantiate();
        foreach (IDTSVirtualInputColumn100 v in vInput.VirtualInputColumnCollection) {
            var col = design.SetUsageType(input.ID, vInput, v.LineageID, DTSUsageType.UT_READONLY);
            design.MapInputColumn(input.ID, col.ID, input.ExternalMetadataColumnCollection[v.Name].ID);
        }
        silver.PackageItems.Add(copy, "Load_Customer.dtsx");
        var order = New("Load_Order");
        Sql(order, Cm(order, "Target", "LABSILVER", "TargetDb"), "Orders",
            "INSERT INTO dbo.Orders (Id) SELECT c.Id FROM dbo.Customer c");
        silver.PackageItems.Add(order, "Load_Order.dtsx");
        silver.SaveTo(dir + "\\SILVER.ispac");
        silver.Dispose();

        var gold = NewProject("GOLD", "TargetDb");
        var dim = New("Dim_Customer");
        Sql(dim, Cm(dim, "Target", "LABGOLD", "TargetDb"), "Dimension",
            "INSERT INTO dbo.DimCustomer (Id) SELECT Id FROM LABSILVER.dbo.Customer");
        gold.PackageItems.Add(dim, "Dim_Customer.dtsx");
        var own = New("Load_Customer");
        Sql(own, Cm(own, "Target", "LABGOLD", "TargetDb"), "Gold customers",
            "INSERT INTO dbo.Customer (Id) SELECT Id FROM dbo.DimCustomer");
        gold.PackageItems.Add(own, "Load_Customer.dtsx");
        gold.SaveTo(dir + "\\GOLD.ispac");
        gold.Dispose();
    }
}
'@
$dir = 'C:\lab\work\medallion'
Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $dir | Out-Null
[Medallion]::Build($dir)
Write-Output "MEDALLION BUILT $((Get-ChildItem $dir -Filter *.ispac).Count) projects"
