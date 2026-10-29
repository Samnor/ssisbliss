# Run onprem-ssis as a plain Windows user with only folder permissions on SILVER (no sysadmin, no ssis_admin):
# pull, plan and apply one package deploy, then put SILVER back where it was.
# Usage: lab.py install-tools; lab.py run lab/tools/devlogin-ssis.ps1
$ErrorActionPreference = 'Stop'
$sqlcmd = Get-ChildItem 'C:\Program Files\Microsoft SQL Server\Client SDK\ODBC' -Recurse -Filter SQLCMD.EXE | Select-Object -First 1 -ExpandProperty FullName
$port = $Lab.instances[0].port
$login = "$env:COMPUTERNAME\labdev"
function Sql([string]$q) { $o = & $sqlcmd -S "localhost,$port" -E -b -h -1 -W -Q $q; if ($LASTEXITCODE) { throw "sql failed: $o" }; $o }

# Local user with a throwaway password that only lives in this script run.
$pw = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 20 | ForEach-Object { [char]$_ }) + '!a1'
$secure = ConvertTo-SecureString $pw -AsPlainText -Force
if (Get-LocalUser labdev -ErrorAction SilentlyContinue) { Set-LocalUser labdev -Password $secure } else { New-LocalUser labdev -Password $secure -PasswordNeverExpires | Out-Null }
Add-LocalGroupMember -Group Users -Member labdev -ErrorAction SilentlyContinue
# A scheduled task with a stored password needs "Log on as a batch job" (SeBatchLogonRight).
$sid = (Get-LocalUser labdev).SID.Value
secedit /export /cfg C:\lab\work\secpol.inf /areas USER_RIGHTS | Out-Null
$cfg = Get-Content C:\lab\work\secpol.inf
if (-not ($cfg | Select-String "SeBatchLogonRight.*$sid")) {
    $line = $cfg | Where-Object { $_ -like 'SeBatchLogonRight*' }
    $cfg = if ($line) { $cfg -replace '^SeBatchLogonRight = (.*)$', "SeBatchLogonRight = `$1,*$sid" }
           else { $cfg -replace '^\[Privilege Rights\]$', "[Privilege Rights]`r`nSeBatchLogonRight = *$sid" }
    $cfg | Set-Content C:\lab\work\secpol.inf
    secedit /configure /db C:\lab\work\secpol.sdb /cfg C:\lab\work\secpol.inf /areas USER_RIGHTS | Out-Null
}
$work = 'C:\lab\work\labdev'
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $work | Out-Null
icacls $work /grant "labdev:(OI)(CI)M" | Out-Null

Sql @"
IF SUSER_ID(N'$login') IS NULL CREATE LOGIN [$login] FROM WINDOWS;
USE SSISDB;
IF USER_ID(N'labdev') IS NULL CREATE USER [labdev] FOR LOGIN [$login];
DECLARE @f bigint = (SELECT folder_id FROM catalog.folders WHERE name = N'SILVER'), @u int = USER_ID(N'labdev'), @t int;
DECLARE t CURSOR FOR SELECT v FROM (VALUES (1),(101),(102),(103)) x(v);  -- READ, READ/MODIFY/EXECUTE_OBJECTS
OPEN t; FETCH t INTO @t;
WHILE @@FETCH_STATUS = 0 BEGIN EXEC catalog.grant_permission 1, @f, @u, @t; FETCH t INTO @t; END
CLOSE t; DEALLOCATE t;
"@ | Out-Null
$startLsn = (Sql "SET NOCOUNT ON; SELECT p.object_version_lsn FROM SSISDB.catalog.projects p JOIN SSISDB.catalog.folders f ON f.folder_id = p.folder_id WHERE f.name = N'SILVER' AND p.name = N'SILVER'").Trim()

$job = @"
`$ErrorActionPreference = 'Continue'
Set-Location $work
`$t = 'C:\lab\tools\venv\Scripts\onprem-ssis.exe'
`$a = @('--server', 'localhost,$port', '--trust-server-certificate', '--folder', 'SILVER', '--project', 'SILVER')
"whoami: `$(whoami)" | Out-File out.txt
"=== pull" | Out-File -Append out.txt
& `$t pull @a --out SILVER --packages Stage_Customer.dtsx 2>&1 | Out-File -Append out.txt
[xml]`$x = Get-Content -Raw SILVER\Stage_Customer.dtsx
[void]`$x.DocumentElement.SetAttribute('Description', 'www.microsoft.com/SqlServer/Dts', 'labdev change')
`$x.Save("$work\SILVER\Stage_Customer.dtsx")
"=== plan" | Out-File -Append out.txt
`$plan = & `$t deploy-packages @a SILVER\Stage_Customer.dtsx 2>&1 | Out-String
`$plan | Out-File -Append out.txt
`$id = (`$plan | ConvertFrom-Json).plan_id
if (`$id) {
  "=== apply" | Out-File -Append out.txt
  & `$t deploy-packages @a SILVER\Stage_Customer.dtsx --apply --plan-id `$id --backup-dir $work\bk 2>&1 | Out-File -Append out.txt
}
"=== done" | Out-File -Append out.txt
"@
Set-Content -Path "$work\job.ps1" -Value $job
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File $work\job.ps1"
Register-ScheduledTask -TaskName onprem-labdev -Action $action -User $login -Password $pw -RunLevel Limited -Force | Out-Null
Start-ScheduledTask -TaskName onprem-labdev
$deadline = (Get-Date).AddMinutes(5)
function Done { (Test-Path "$work\out.txt") -and (Select-String -Path "$work\out.txt" -Pattern '=== done' -Quiet) }
while ((Get-Date) -lt $deadline -and -not (Done)) { Start-Sleep 3 }
$info = Get-ScheduledTaskInfo -TaskName onprem-labdev
Write-Output "task last result: $($info.LastTaskResult)" 
Unregister-ScheduledTask -TaskName onprem-labdev -Confirm:$false
Get-Content "$work\out.txt" -Raw -Encoding Unicode -ErrorAction SilentlyContinue
$endLsn = (Sql "SET NOCOUNT ON; SELECT p.object_version_lsn FROM SSISDB.catalog.projects p JOIN SSISDB.catalog.folders f ON f.folder_id = p.folder_id WHERE f.name = N'SILVER' AND p.name = N'SILVER'").Trim()
Write-Output "lsn before $startLsn, after $endLsn"
if ($endLsn -ne $startLsn) {
    # While labdev's deploy is live, rolling back past it (as this script's login) must be refused.
    $rb = & C:\lab\tools\venv\Scripts\onprem-ssis.exe rollback --server "localhost,$port" --trust-server-certificate --folder SILVER --project SILVER --to-lsn $startLsn | Out-String | ConvertFrom-Json
    $others = ($rb.checks | Where-Object { $_.check -eq 'others' }).detail
    Write-Output "others check: $others"
    Sql "EXEC SSISDB.catalog.restore_project @folder_name = N'SILVER', @project_name = N'SILVER', @object_version_lsn = $startLsn" | Out-Null
    Write-Output "restored SILVER to lsn $startLsn"
}
