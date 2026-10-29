# Shared helpers for seed scripts. lab.py prepends this file after $Lab.
$ErrorActionPreference = 'Stop'

$script:SqlCmdExe = Get-ChildItem 'C:\Program Files\Microsoft SQL Server\Client SDK\ODBC' -Recurse -Filter SQLCMD.EXE |
    Select-Object -First 1 -ExpandProperty FullName

function Invoke-LabSql([object]$Instance, [string]$Query, [string]$Database = 'master') {
    $file = [IO.Path]::GetTempFileName()
    Set-Content -Path $file -Value $Query -Encoding UTF8
    try {
        $out = & $script:SqlCmdExe -S "localhost,$($Instance.port)" -E -b -d $Database -h -1 -W -i $file 2>&1
        if ($LASTEXITCODE -ne 0) { throw "SQL failed on $($Instance.name)/${Database}: $($out -join "`n")" }
        return $out
    }
    finally { Remove-Item $file -ErrorAction SilentlyContinue }
}

function Get-LabSeedInstances {
    # Every instance gets the estate: they stand in for separate dev/stage/prod servers.
    $Lab.instances
}

function Add-LabAssembly([string]$Name) {
    Add-Type -AssemblyName "$Name, Version=15.0.0.0, Culture=neutral, PublicKeyToken=89845dcd8080cc91"
}
