# Install every instance listed in $Lab.instances from the downloaded ISO.
$ErrorActionPreference = 'Stop'

$sa = (Get-SSMParameter -Name $Lab.sa_password_parameter -WithDecryption $true -Region $Lab.region).Value

$iso = Get-ChildItem 'C:\lab\media' -Filter *.iso | Select-Object -First 1
if (-not $iso) { throw 'No ISO in C:\lab\media; run 01-download.ps1 first' }
$mount = Mount-DiskImage -ImagePath $iso.FullName -PassThru
$drive = ($mount | Get-Volume).DriveLetter
$setup = "${drive}:\setup.exe"

try {
    foreach ($inst in $Lab.instances) {
        $svc = Get-Service -Name "MSSQL`$$($inst.name)" -ErrorAction SilentlyContinue
        if ($svc) { Write-Output "$($inst.name) already installed, skipping"; continue }

        Write-Output "Installing $($inst.name) with $($inst.features)"
        $setupArgs = @(
            '/Q', '/ACTION=Install', '/IACCEPTSQLSERVERLICENSETERMS', '/SUPPRESSPRIVACYSTATEMENTNOTICE',
            '/UPDATEENABLED=False',
            "/FEATURES=$($inst.features)",
            "/INSTANCENAME=$($inst.name)",
            '/AGTSVCSTARTUPTYPE=Automatic',
            '/SECURITYMODE=SQL', "/SAPWD=`"$sa`"",
            '/SQLSYSADMINACCOUNTS="BUILTIN\Administrators"',
            '/TCPENABLED=1'
        )
        if ($inst.features -match '\bAS\b') {
            $setupArgs += "/ASSERVERMODE=$($Lab.ssas_mode)", '/ASSYSADMINACCOUNTS="BUILTIN\Administrators"'
        }
        $p = Start-Process -FilePath $setup -ArgumentList $setupArgs -Wait -PassThru -NoNewWindow
        # 3010 = success, reboot required
        if ($p.ExitCode -notin 0, 3010) {
            $summary = Get-ChildItem 'C:\Program Files\Microsoft SQL Server\150\Setup Bootstrap\Log\Summary.txt' -ErrorAction SilentlyContinue
            if ($summary) { Get-Content $summary.FullName -Tail 60 }
            throw "Setup for $($inst.name) failed with exit code $($p.ExitCode)"
        }

        # Pin a static TCP port so tunnels and connection strings are predictable.
        $tcp = "HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\$($Lab.instance_id_prefix).$($inst.name)\MSSQLServer\SuperSocketNetLib\Tcp\IPAll"
        Set-ItemProperty -Path $tcp -Name TcpDynamicPorts -Value ''
        Set-ItemProperty -Path $tcp -Name TcpPort -Value ([string]$inst.port)
        Restart-Service -Name "MSSQL`$$($inst.name)" -Force
        Start-Service -Name "SQLAgent`$$($inst.name)"
    }
}
finally {
    Dismount-DiskImage -ImagePath $iso.FullName | Out-Null
}

Get-Service MSSQL*, SQLAgent*, MSOLAP*, MsDtsServer* -ErrorAction SilentlyContinue |
    Format-Table Name, Status, StartType -AutoSize | Out-String
