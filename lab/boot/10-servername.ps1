# Make @@SERVERNAME match the computer name. A launched copy of an image can get a
# new computer name, which breaks SSIS, linked servers and anything using @@SERVERNAME.
$ErrorActionPreference = 'Stop'

$sqlcmd = Get-ChildItem 'C:\Program Files\Microsoft SQL Server\Client SDK\ODBC' -Recurse -Filter SQLCMD.EXE |
    Select-Object -First 1 -ExpandProperty FullName

foreach ($inst in $Lab.instances) {
    $want = "$env:COMPUTERNAME\$($inst.name)"
    $have = (& $sqlcmd -S "localhost,$($inst.port)" -E -b -h -1 -W -Q 'SET NOCOUNT ON; SELECT @@SERVERNAME') |
        Select-Object -First 1
    if ($LASTEXITCODE -ne 0) { throw "cannot query $($inst.name): $have" }
    if ($have.Trim() -eq $want) { Write-Output "$($inst.name): @@SERVERNAME is $want"; continue }

    Write-Output "$($inst.name): @@SERVERNAME is $have, renaming to $want"
    & $sqlcmd -S "localhost,$($inst.port)" -E -b -Q "EXEC sp_dropserver N'$($have.Trim())'; EXEC sp_addserver N'$want', 'local';"
    if ($LASTEXITCODE -ne 0) { throw "rename failed for $($inst.name)" }
    Restart-Service -Name "MSSQL`$$($inst.name)" -Force
    Start-Service -Name "SQLAgent`$$($inst.name)"
}
