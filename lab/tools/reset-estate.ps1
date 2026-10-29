# Remove the mock estate from every lab instance, so `lab.py seed` can lay down estate.json from scratch
# (after renaming things in it, say). Drops user databases (not SSISDB or MDS), every SSISDB folder with
# its projects, environments and logs, and every Agent job except SQL Server's own.
# Usage: lab.py run lab/tools/reset-estate.ps1; then lab.py seed
$ErrorActionPreference = 'Stop'
$sqlcmd = Get-ChildItem 'C:\Program Files\Microsoft SQL Server\Client SDK\ODBC' -Recurse -Filter SQLCMD.EXE | Select-Object -First 1 -ExpandProperty FullName
function Sql([object]$inst, [string]$q) {
    $file = [IO.Path]::GetTempFileName()
    Set-Content -Path $file -Value $q -Encoding UTF8
    try {
        $o = & $sqlcmd -S "localhost,$($inst.port)" -E -b -h -1 -W -i $file 2>&1
        if ($LASTEXITCODE) { throw "sql failed on $($inst.name): $($o -join "`n")" }
        $o
    } finally { Remove-Item $file -ErrorAction SilentlyContinue }
}

foreach ($inst in $Lab.instances) {
    Sql $inst @"
SET NOCOUNT ON;
DECLARE @j sysname;
DECLARE jobs CURSOR LOCAL FOR SELECT name FROM msdb.dbo.sysjobs WHERE name NOT IN (N'syspolicy_purge_history', N'SSIS Server Maintenance Job');
OPEN jobs; FETCH jobs INTO @j;
WHILE @@FETCH_STATUS = 0 BEGIN EXEC msdb.dbo.sp_delete_job @job_name = @j; PRINT CONCAT('job: ', @j); FETCH jobs INTO @j; END
CLOSE jobs; DEALLOCATE jobs;

IF DB_ID(N'SSISDB') IS NOT NULL
BEGIN
    DECLARE @f sysname, @n sysname;
    DECLARE projects CURSOR LOCAL FOR SELECT f.name, p.name FROM SSISDB.catalog.projects p
        JOIN SSISDB.catalog.folders f ON f.folder_id = p.folder_id;
    OPEN projects; FETCH projects INTO @f, @n;
    WHILE @@FETCH_STATUS = 0 BEGIN EXEC SSISDB.catalog.delete_project @folder_name = @f, @project_name = @n; FETCH projects INTO @f, @n; END
    CLOSE projects; DEALLOCATE projects;
    DECLARE envs CURSOR LOCAL FOR SELECT f.name, e.name FROM SSISDB.catalog.environments e
        JOIN SSISDB.catalog.folders f ON f.folder_id = e.folder_id;
    OPEN envs; FETCH envs INTO @f, @n;
    WHILE @@FETCH_STATUS = 0 BEGIN EXEC SSISDB.catalog.delete_environment @folder_name = @f, @environment_name = @n; FETCH envs INTO @f, @n; END
    CLOSE envs; DEALLOCATE envs;
    DECLARE folders CURSOR LOCAL FOR SELECT name FROM SSISDB.catalog.folders;
    OPEN folders; FETCH folders INTO @f;
    WHILE @@FETCH_STATUS = 0 BEGIN EXEC SSISDB.catalog.delete_folder @folder_name = @f; PRINT CONCAT('folder: ', @f); FETCH folders INTO @f; END
    CLOSE folders; DEALLOCATE folders;
    -- Operation and execution logs still name the old folders and packages (child rows cascade).
    DELETE SSISDB.internal.operations;
    PRINT 'ssisdb logs cleared';
END

DECLARE @d sysname, @cdc bit, @sql nvarchar(max);
DECLARE dbs CURSOR LOCAL FOR SELECT name, is_cdc_enabled FROM sys.databases
    WHERE database_id > 4 AND name <> N'SSISDB' AND name NOT LIKE N'MDS%';
OPEN dbs; FETCH dbs INTO @d, @cdc;
WHILE @@FETCH_STATUS = 0
BEGIN
    IF @cdc = 1 BEGIN SET @sql = N'USE ' + QUOTENAME(@d) + N'; EXEC sys.sp_cdc_disable_db;'; EXEC (@sql); END
    SET @sql = N'ALTER DATABASE ' + QUOTENAME(@d) + N' SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE ' + QUOTENAME(@d) + N';';
    EXEC (@sql);
    PRINT CONCAT('database: ', @d);
    FETCH dbs INTO @d, @cdc;
END
CLOSE dbs; DEALLOCATE dbs;
"@ | ForEach-Object { "$($inst.name) $_" }
}
Remove-Item -Recurse -Force C:\lab\work -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force C:\lab\work | Out-Null
Write-Output "RESET DONE"
