# Check every instance reports the expected build. Throws on mismatch so the image is not captured.
$ErrorActionPreference = 'Stop'

$sqlcmd = Get-ChildItem 'C:\Program Files\Microsoft SQL Server\Client SDK\ODBC' -Recurse -Filter SQLCMD.EXE |
    Select-Object -First 1 -ExpandProperty FullName
$failed = @()
foreach ($inst in $Lab.instances) {
    $q = "SET NOCOUNT ON; SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(20)) + '|' + CAST(SERVERPROPERTY('Edition') AS varchar(60))"
    $out = & $sqlcmd -S "localhost,$($inst.port)" -E -h -1 -W -Q $q
    if ($LASTEXITCODE -ne 0) { throw "sqlcmd failed for $($inst.name): $out" }
    $version, $edition = ($out | Select-Object -First 1).Trim() -split '\|'
    Write-Output ("{0,-10} {1,-14} {2}" -f $inst.name, $version, $edition)
    if ($version -ne $Lab.build) { $failed += "$($inst.name) is $version, expected $($Lab.build)" }
}
if ($failed) { throw ($failed -join '; ') }
Write-Output "All instances at $($Lab.build)"
