# Set sa on every instance to the password in Parameter Store, so a machine launched
# from any saved image has the current lab password. The value is read on the host and
# never passes through the SSM command text.
$ErrorActionPreference = 'Stop'

$sa = (Get-SSMParameter -Name $Lab.sa_password_parameter -WithDecryption $true -Region $Lab.region).Value
$sqlcmd = Get-ChildItem 'C:\Program Files\Microsoft SQL Server\Client SDK\ODBC' -Recurse -Filter SQLCMD.EXE |
    Select-Object -First 1 -ExpandProperty FullName

$env:LAB_SA = $sa
foreach ($inst in $Lab.instances) {
    # sqlcmd scripting variables keep the password out of the process command line.
    & $sqlcmd -S "localhost,$($inst.port)" -E -b -Q "ALTER LOGIN sa WITH PASSWORD = N'`$(LAB_SA)', CHECK_POLICY = OFF; ALTER LOGIN sa ENABLE;"
    if ($LASTEXITCODE -ne 0) { throw "setting sa failed on $($inst.name)" }
    Write-Output "$($inst.name): sa password set"
}
Remove-Item Env:LAB_SA
