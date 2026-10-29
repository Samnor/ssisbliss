# Mock databases: a load log in each, a few tables with rows, CDC, read-only history databases.
foreach ($inst in Get-LabSeedInstances) {
    foreach ($db in $Lab.estate.databases) {
        $name = $db.name
        Invoke-LabSql $inst @"
IF DB_ID(N'$name') IS NULL CREATE DATABASE [$name];
ALTER DATABASE [$name] SET RECOVERY SIMPLE;
"@ | Out-Null

        Invoke-LabSql $inst @"
IF OBJECT_ID('dbo.LoadLog') IS NULL
    CREATE TABLE dbo.LoadLog (LoadLogId int IDENTITY PRIMARY KEY, Package sysname NOT NULL,
                              LoadedAt datetime2 NOT NULL DEFAULT SYSDATETIME(), RowsLoaded int NOT NULL);
-- Deterministic data from row numbers (CHOOSE with NEWID() can yield NULL: it re-evaluates per branch).
IF OBJECT_ID('dbo.Customer') IS NULL
    CREATE TABLE dbo.Customer (CustomerId int PRIMARY KEY, Name nvarchar(100) NOT NULL, Region nvarchar(50) NOT NULL);
IF NOT EXISTS (SELECT 1 FROM dbo.Customer)
    INSERT dbo.Customer SELECT i, CONCAT(N'Customer ', i), CHOOSE(i % 4 + 1, N'North', N'South', N'East', N'West')
    FROM (SELECT TOP (500) ROW_NUMBER() OVER (ORDER BY (SELECT 1)) AS i FROM sys.all_objects) n;
IF OBJECT_ID('dbo.Product') IS NULL
    CREATE TABLE dbo.Product (ProductId int PRIMARY KEY, Name nvarchar(100) NOT NULL, Price decimal(10,2) NOT NULL);
IF NOT EXISTS (SELECT 1 FROM dbo.Product)
    INSERT dbo.Product SELECT i, CONCAT(N'Product ', i), (i * 37) % 1000 + 1
    FROM (SELECT TOP (100) ROW_NUMBER() OVER (ORDER BY (SELECT 1)) AS i FROM sys.all_objects) n;
IF OBJECT_ID('dbo.SalesOrder') IS NULL
    CREATE TABLE dbo.SalesOrder (SalesOrderId int PRIMARY KEY, CustomerId int NOT NULL, ProductId int NOT NULL,
                                 OrderDate date NOT NULL, Quantity int NOT NULL);
IF NOT EXISTS (SELECT 1 FROM dbo.SalesOrder)
    INSERT dbo.SalesOrder SELECT i, (i * 7) % 500 + 1, (i * 13) % 100 + 1,
        DATEADD(day, -((i * 31) % 730), CAST(SYSDATETIME() AS date)), i % 20 + 1
    FROM (SELECT TOP (5000) ROW_NUMBER() OVER (ORDER BY (SELECT 1)) AS i FROM sys.all_objects a CROSS JOIN sys.all_objects b) n;
"@ -Database $name | Out-Null

        if ($db.cdc_tables) {
            Invoke-LabSql $inst "IF (SELECT is_cdc_enabled FROM sys.databases WHERE name = DB_NAME()) = 0 EXEC sys.sp_cdc_enable_db;" -Database $name | Out-Null
            foreach ($t in $db.cdc_tables) {
                Invoke-LabSql $inst @"
IF NOT EXISTS (SELECT 1 FROM cdc.change_tables WHERE source_object_id = OBJECT_ID(N'dbo.$t'))
    EXEC sys.sp_cdc_enable_table @source_schema = N'dbo', @source_name = N'$t', @role_name = NULL;
"@ -Database $name | Out-Null
            }
        }
        if ($db.read_only) {
            Invoke-LabSql $inst "IF DATABASEPROPERTYEX(N'$name', 'Updateability') = 'READ_WRITE' ALTER DATABASE [$name] SET READ_ONLY WITH ROLLBACK IMMEDIATE;" | Out-Null
        }
    }
    Write-Output "$($inst.name): $($Lab.estate.databases.Count) databases ready"
}
