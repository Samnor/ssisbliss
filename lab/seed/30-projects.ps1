# Build real SSIS projects with the SSIS object model, deploy them, and give each folder an
# environment bound to the project parameters - the shape the deploy tools must preserve.
Add-LabAssembly 'Microsoft.SqlServer.ManagedDTS'
Add-LabAssembly 'Microsoft.SqlServer.Management.IntegrationServices'
$IS = 'Microsoft.SqlServer.Management.IntegrationServices'
$work = 'C:\lab\seed'
New-Item -ItemType Directory -Force -Path $work, 'C:\lab\scripts' | Out-Null

# Scripts the import-layer packages run through Execute Process tasks.
@'
# Fetch prices from a vendor API into a CSV for the import packages (the host has no such API: fails).
import sys, urllib.request
URL = "https://prices.api.invalid/v1/daily"
try:
    with urllib.request.urlopen(URL, timeout=10) as r:
        open("prices.csv", "wb").write(r.read())
except Exception as e:
    print(f"fetch failed: {URL}: {e}", file=sys.stderr)
    sys.exit(2)
'@ | Set-Content -Encoding ascii C:\lab\scripts\fetch_prices.py
@'
# Move processed import files to the archive folder.
$inbox = 'C:\lab\scripts\inbox'; $archive = 'C:\lab\scripts\archive'
New-Item -ItemType Directory -Force -Path $inbox, $archive | Out-Null
Get-ChildItem $inbox -File | Move-Item -Destination $archive -Force
Write-Output "archived"
'@ | Set-Content -Encoding ascii C:\lab\scripts\archive_files.ps1

function New-LabIspac($p, [string]$path) {
    $project = [Microsoft.SqlServer.Dts.Runtime.Project]::CreateProject()
    $project.Name = $p.project
    $project.ProtectionLevel = [Microsoft.SqlServer.Dts.Runtime.DTSProtectionLevel]::DontSaveSensitive

    foreach ($param in @(@('TargetServer', 'localhost'), @('TargetDb', $p.target_db), @('LoadMode', 'Delta'))) {
        $pp = $project.Parameters.Add($param[0], [TypeCode]::String)
        $pp.Value = $param[1]
    }
    if ($p.project -eq 'OUTBOUND') {
        $secret = $project.Parameters.Add('ApiKey', [TypeCode]::String)
        $secret.Sensitive = $true
    }
    $meta = $project.ConnectionManagerItems.Add('OLEDB', 'Metadata.conmgr')
    $meta.ConnectionManager.Name = 'Metadata'
    $meta.ConnectionManager.ConnectionString = 'Data Source=localhost;Initial Catalog=CONTROL;Provider=MSOLEDBSQL;Integrated Security=SSPI;'

    foreach ($entry in $p.packages) {
        # A package is a name (one SQL task) or {name, tasks: [{sql|powershell|python: ...}]}, run in order.
        $name = if ($entry -is [string]) { $entry } else { $entry.name }
        $tasks = if ($entry -is [string]) {
            @(@{ name = "Load $name"; sql = "INSERT dbo.LoadLog (Package, RowsLoaded) SELECT N'$name', COUNT(*) FROM dbo.SalesOrder;" })
        } else { @($entry.tasks) }
        $pkg = New-Object Microsoft.SqlServer.Dts.Runtime.Package
        $pkg.Name = $name
        $pkg.ProtectionLevel = [Microsoft.SqlServer.Dts.Runtime.DTSProtectionLevel]::DontSaveSensitive
        $cm = $pkg.Connections.Add('OLEDB')
        $cm.Name = 'Target'
        $cm.ConnectionString = "Data Source=localhost;Initial Catalog=$($p.target_db);Provider=MSOLEDBSQL;Integrated Security=SSPI;"
        $cm.SetExpression('ConnectionString',
            '"Data Source=" + @[$Project::TargetServer] + ";Initial Catalog=" + @[$Project::TargetDb] + ";Provider=MSOLEDBSQL;Integrated Security=SSPI;"')
        $previous = $null
        foreach ($t in $tasks) {
            if ($t.sql) {
                $th = [Microsoft.SqlServer.Dts.Runtime.TaskHost]$pkg.Executables.Add('STOCK:SQLTask')
                $th.Properties['Connection'].SetValue($th, $cm.ID)
                $th.Properties['SqlStatementSource'].SetValue($th, $t.sql)
            } else {
                $th = [Microsoft.SqlServer.Dts.Runtime.TaskHost]$pkg.Executables.Add('STOCK:ExecuteProcessTask')
                if ($t.powershell) {
                    $exe = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
                    $arguments = "-NoProfile -ExecutionPolicy Bypass -File C:\lab\scripts\$($t.powershell)"
                } else {
                    $exe = 'C:\Python312\python.exe'   # installed by lab.py install-tools
                    $arguments = "C:\lab\scripts\$($t.python)"
                }
                $th.Properties['Executable'].SetValue($th, $exe)
                $th.Properties['Arguments'].SetValue($th, $arguments)
                $th.Properties['WorkingDirectory'].SetValue($th, 'C:\lab\scripts')
            }
            $th.Name = $t.name
            if ($previous) { [void]$pkg.PrecedenceConstraints.Add($previous, $th) }
            $previous = $th
        }
        [void]$project.PackageItems.Add($pkg, "$name.dtsx")
    }
    $project.SaveTo($path)
    $project.Dispose()
}

foreach ($inst in Get-LabSeedInstances) {
    $conn = New-Object System.Data.SqlClient.SqlConnection "Data Source=localhost,$($inst.port);Initial Catalog=master;Integrated Security=SSPI;"
    $catalog = (New-Object "$IS.IntegrationServices" $conn).Catalogs['SSISDB']
    if (-not $catalog) { throw "$($inst.name): no SSISDB; run 20-ssisdb.ps1 first" }

    foreach ($p in $Lab.estate.projects) {
        $folder = $catalog.Folders[$p.folder]
        if (-not $folder) {
            $folder = New-Object "$IS.CatalogFolder" ($catalog, $p.folder, "$($p.folder) layer")
            $folder.Create()
        }
        if (-not $folder.Projects[$p.project]) {
            $path = Join-Path $work "$($p.project).ispac"
            New-LabIspac $p $path
            [void]$folder.DeployProject($p.project, [IO.File]::ReadAllBytes($path))
            $folder.Refresh()
        }

        $envInfo = $folder.Environments['Default']
        if (-not $envInfo) {
            $envInfo = New-Object "$IS.EnvironmentInfo" ($folder, 'Default', 'Values for this server')
            $envInfo.Create()
            $envInfo.Variables.Add('TargetServer', [TypeCode]::String, "$env:COMPUTERNAME\$($inst.name)", $false, '')
            $envInfo.Variables.Add('TargetDb', [TypeCode]::String, $p.target_db, $false, '')
            if ($p.project -eq 'OUTBOUND') { $envInfo.Variables.Add('ApiKey', [TypeCode]::String, 'lab-not-a-secret', $true, '') }
            $envInfo.Alter()
        }

        $proj = $folder.Projects[$p.project]
        if ($proj.References.Count -eq 0) {
            $proj.References.Add('Default')
            $proj.Alter()
            $proj.Refresh()
        }
        $referenced = [Type]"$IS.ParameterInfo+ParameterValueType"
        foreach ($name in 'TargetServer', 'TargetDb') { $proj.Parameters[$name].Set($referenced::Referenced, $name) }
        if ($p.project -eq 'OUTBOUND') { $proj.Parameters['ApiKey'].Set($referenced::Referenced, 'ApiKey') }
        $proj.Parameters['LoadMode'].Set($referenced::Literal, 'Full')
        $proj.Alter()
        Write-Output "$($inst.name): $($p.folder)/$($p.project) with $($p.packages.Count) packages"
    }
}
