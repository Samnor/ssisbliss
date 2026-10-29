# SQL Agent: a numbered job chain running each layer's Main package, plus the usual extras
# (guard job, event variant, failing and disabled jobs). Runs the chain once for history.
$ssisCommand = '/ISSERVER "\"\SSISDB\{FOLDER}\{PROJECT}\Main.dtsx\"" /SERVER "\"{SERVER}\"" /ENVREFERENCE {REF} ' +
               '/Par "\"$ServerOption::LOGGING_LEVEL(Int16)\"";1 /Par "\"$ServerOption::SYNCHRONIZED(Boolean)\"";True ' +
               '/CALLERINFO SQLAGENT /REPORTING E'

function Add-LabJob($inst, [string]$name, [array]$steps, [bool]$enabled = $true) {
    $exists = Invoke-LabSql $inst "SET NOCOUNT ON; SELECT COUNT(*) FROM msdb.dbo.sysjobs WHERE name = N'$name'"
    if ([int]($exists | Select-Object -First 1) -gt 0) { return }
    $sql = "EXEC msdb.dbo.sp_add_job @job_name = N'$name', @enabled = $([int]$enabled), @owner_login_name = N'sa';`n"
    $i = 0
    foreach ($s in $steps) {
        $i++
        $last = $i -eq $steps.Count
        $onSuccess = if ($last) { 1 } else { 3 }   # 1 = quit with success, 3 = next step
        $cmd = $s.command.Replace("'", "''")
        $sql += "EXEC msdb.dbo.sp_add_jobstep @job_name = N'$name', @step_name = N'$($s.name)', @subsystem = N'$($s.subsystem)', " +
                "@command = N'$cmd', @on_success_action = $onSuccess, @on_fail_action = 2;`n"
    }
    $sql += "EXEC msdb.dbo.sp_add_jobserver @job_name = N'$name';"
    Invoke-LabSql $inst $sql | Out-Null
}

function Get-LabSsisStep($inst, [string]$project, $params = $null, [string]$package = 'Main') {
    $p = $Lab.estate.projects | Where-Object project -eq $project
    $ref = Invoke-LabSql $inst @"
SET NOCOUNT ON;
SELECT r.reference_id FROM catalog.environment_references r
JOIN catalog.projects p ON p.project_id = r.project_id JOIN catalog.folders f ON f.folder_id = p.folder_id
WHERE f.name = N'$($p.folder)' AND p.name = N'$project' AND r.environment_name = N'Default'
"@ -Database 'SSISDB'
    $command = $ssisCommand.Replace('Main.dtsx', "$package.dtsx").Replace('{FOLDER}', $p.folder).Replace('{PROJECT}', $project).
        Replace('{SERVER}', "$env:COMPUTERNAME\$($inst.name)").Replace('{REF}', ($ref | Select-Object -First 1).Trim())
    if ($params) {   # project parameter overrides, e.g. a target that makes the package fail
        foreach ($k in $params.PSObject.Properties) { $command += ' /Par "\"$Project::' + $k.Name + '\"";' + $k.Value }
    }
    @{ name = "Run $project $package"; subsystem = 'SSIS'; command = $command }
}

foreach ($inst in Get-LabSeedInstances) {
    $chain = @($Lab.estate.chain)
    for ($i = 0; $i -lt $chain.Count; $i++) {
        $steps = @(Get-LabSsisStep $inst $chain[$i])
        if ($i + 1 -lt $chain.Count) {
            $steps += @{ name = 'Start next'; subsystem = 'TSQL'; command = "EXEC msdb.dbo.sp_start_job @job_name = N'$($i + 2) $($chain[$i + 1])';" }
        }
        Add-LabJob $inst "$($i + 1) $($chain[$i])" $steps
    }

    foreach ($j in $Lab.estate.extra_jobs) {
        $enabled = if ($null -eq $j.enabled) { $true } else { [bool]$j.enabled }
        $steps = switch ($j.kind) {
            'ssis'    { @(Get-LabSsisStep $inst $j.project $j.params ($(if ($j.package) { $j.package } else { 'Main' }))) }
            'guard'   { @(@{ name = 'Check'; subsystem = 'TSQL'; command = "IF EXISTS (SELECT 1 FROM msdb.dbo.sysjobactivity a JOIN msdb.dbo.sysjobs j ON j.job_id = a.job_id WHERE a.start_execution_date IS NOT NULL AND a.stop_execution_date IS NULL AND j.name LIKE N'[0-9] %') RAISERROR(N'Load in progress', 16, 1);" }) }
            'failing' { @(@{ name = 'Work'; subsystem = 'TSQL'; command = "RAISERROR(N'Simulated failure: source unavailable', 16, 1);" }) }
            default   { @(@{ name = 'Work'; subsystem = 'TSQL'; command = 'SELECT 1;' }) }
        }
        Add-LabJob $inst $j.name $steps $enabled
    }

    Invoke-LabSql $inst "EXEC msdb.dbo.sp_start_job @job_name = N'1 $($chain[0])'; EXEC msdb.dbo.sp_start_job @job_name = N'Dimensions on load stop'; EXEC msdb.dbo.sp_start_job @job_name = N'SILVER into history'; EXEC msdb.dbo.sp_start_job @job_name = N'Fetch from price API';" | Out-Null
    Write-Output "$($inst.name): $($chain.Count + $Lab.estate.extra_jobs.Count) jobs; chain started"
}
