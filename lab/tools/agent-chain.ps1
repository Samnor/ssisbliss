# ssisbliss against the real SQL Agent: a chain broken by a read-only target, and a chain that stops
# because a job quits before its Start next step. Restores both. Prints one JSON line.
$ErrorActionPreference = 'Stop'
$sqlcmd = Get-ChildItem "C:\Program Files\Microsoft SQL Server\Client SDK\ODBC" -Recurse -Filter SQLCMD.EXE | Select-Object -First 1 -ExpandProperty FullName
function Sql($q) { $o = & $sqlcmd -S localhost,14331 -E -b -h -1 -W -Q $q; if ($LASTEXITCODE) { throw "sql failed: $o" }; $o }
$agent = 'C:\lab\tools\venv\Scripts\ssisbliss.exe'; $ssis = 'C:\lab\tools\venv\Scripts\ssisbliss.exe'
$a = @('--server', 'localhost,14331', '--trust-server-certificate')
$out = @{}
Sql "EXEC SSISDB.catalog.set_environment_variable_value @folder_name=N'SILVER', @environment_name=N'Default', @variable_name=N'TargetDb', @value=N'GOLD_ARCHIVE'; EXEC msdb.dbo.sp_start_job @job_name=N'1 BRONZE_API';" | Out-Null
try {
    Start-Sleep 45
    $chain = (& $agent chain @a | Out-String | ConvertFrom-Json).chains | Where-Object root -eq '1 BRONZE_API'
    $out.broken_verdict = $chain.verdict
    $hist = (& $agent history @a '3 SILVER' --runs 1 | Out-String | ConvertFrom-Json).runs[0].steps[0]
    $out.history_next = $hist.next
    $ex = (& $ssis executions @a --id $hist.ssis_execution_id | Out-String | ConvertFrom-Json).executions[0]
    $out.failed_task_kind = $ex.failed_tasks[0].kind
    $out.failed_task_target_db = $ex.failed_tasks[0].connection_parameters.TargetDb
    $out.failed_task_database = $ex.failed_tasks[0].database
} finally {
    Sql "EXEC SSISDB.catalog.set_environment_variable_value @folder_name=N'SILVER', @environment_name=N'Default', @variable_name=N'TargetDb', @value=N'SILVER';" | Out-Null
}
Sql "EXEC msdb.dbo.sp_update_jobstep @job_name = N'2 BRONZE', @step_id = 1, @on_success_action = 1; EXEC msdb.dbo.sp_start_job @job_name = N'1 BRONZE_API';" | Out-Null
try {
    Start-Sleep 30
    $chain = (& $agent chain @a | Out-String | ConvertFrom-Json).chains | Where-Object root -eq '1 BRONZE_API'
    $out.skipped_verdict = $chain.verdict
    $out.skipped_why = $chain.why
} finally {
    Sql "EXEC msdb.dbo.sp_update_jobstep @job_name = N'2 BRONZE', @step_id = 1, @on_success_action = 3;" | Out-Null
}
$out | ConvertTo-Json -Compress
