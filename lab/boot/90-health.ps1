# Fail loudly unless the lab is ready: services up, expected build, SSISDB where seeded.
$ErrorActionPreference = 'Stop'

$sqlcmd = Get-ChildItem 'C:\Program Files\Microsoft SQL Server\Client SDK\ODBC' -Recurse -Filter SQLCMD.EXE |
    Select-Object -First 1 -ExpandProperty FullName
$problems = @()

foreach ($inst in $Lab.instances) {
    foreach ($svc in "MSSQL`$$($inst.name)", "SQLAgent`$$($inst.name)") {
        $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
        if (-not $s -or $s.Status -ne 'Running') { $problems += "$svc is $($s.Status)" }
    }
    $q = "SET NOCOUNT ON; SELECT CONCAT(CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(32)), '|', @@SERVERNAME, '|', " +
         "CASE WHEN DB_ID('SSISDB') IS NULL THEN 0 ELSE 1 END, '|', (SELECT COUNT(*) FROM sys.databases WHERE database_id > 4))"
    $row = (& $sqlcmd -S "localhost,$($inst.port)" -E -b -h -1 -W -Q $q) | Select-Object -First 1
    if ($LASTEXITCODE -ne 0) { $problems += "$($inst.name): query failed: $row"; continue }
    $version, $server, $ssisdb, $dbs = $row.Trim() -split '\|'
    Write-Output ("{0,-8} build {1}  server {2}  SSISDB {3}  user databases {4}" -f $inst.name, $version, $server, $ssisdb, $dbs)
    if ($version -ne $Lab.build) { $problems += "$($inst.name) build $version, expected $($Lab.build)" }
    if ($server -ne "$env:COMPUTERNAME\$($inst.name)") { $problems += "$($inst.name) @@SERVERNAME is $server" }
    if ($Lab.expect_seeded -and $ssisdb -ne '1') { $problems += "$($inst.name) has no SSISDB" }
}

if ($problems) { throw ("lab not healthy: " + ($problems -join '; ')) }
Write-Output 'HEALTHY'
