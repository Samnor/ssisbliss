# End-to-end ssisbliss run against the real SSISDB on the first lab instance, with Windows auth.
# Pulls the SILVER project, edits packages, deploys them both ways, then rolls back to where it started.
# Prints every command's JSON; ends with SMOKE PASSED or throws.
$ErrorActionPreference = 'Stop'
$tool = 'C:\lab\tools\venv\Scripts\ssisbliss.exe'
$py = 'C:\lab\tools\venv\Scripts\python.exe'
$server = "localhost,$($Lab.instances[0].port)"
$work = 'C:\lab\work\smoke'
$backups = 'C:\lab\work\backups'
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $work, $backups | Out-Null
Set-Location $work
$env:SSISBLISS_STATE_DIR = "$work\state"   # this run's own base records
$target = @('--server', $server, '--trust-server-certificate', '--folder', 'SILVER', '--project', 'SILVER')
$failures = @()

function Ssis([string]$label, [string[]]$cliArgs, [int[]]$expect = @(0)) {
    $out = & $tool @cliArgs 2>&1 | Out-String
    $code = $LASTEXITCODE
    # SSM returns at most 24 KB of output: keep each step's full output in smoke.log, print a short excerpt.
    "=== $label (exit $code)`n$($out.Trim())" | Add-Content "$work\smoke.log"
    Write-Host "=== $label (exit $code)"
    Write-Host ($out.Trim().Substring(0, [Math]::Min(400, $out.Trim().Length)))
    if ($code -notin $expect) { $script:failures += "$label exited $code" }
    try { return $out | ConvertFrom-Json } catch { $script:failures += "$label printed no JSON"; return $null }
}

function Edit-Package([string]$path, [string]$note) {
    [xml]$x = Get-Content -Raw $path
    [void]$x.DocumentElement.SetAttribute('Description', 'www.microsoft.com/SqlServer/Dts', $note)
    $x.Save($path)
}

function Plan-Apply([string]$label, [string[]]$cliArgs) {
    $plan = Ssis "$label plan" $cliArgs
    if (-not $plan.plan_id) { $script:failures += "${label}: no plan_id"; return $null }
    $res = Ssis "$label apply" ($cliArgs + @('--apply', '--plan-id', $plan.plan_id, '--backup-dir', $backups))
    if ($res.status -ne 'verified') { $script:failures += "$label status $($res.status)" }
    return $res
}

Ssis 'inspect' @('inspect', '--server', $server, '--trust-server-certificate', '--folder', 'SILVER') | Out-Null
$before = Ssis 'versions before' (@('versions') + $target)
$startLsn = $before.current_lsn
Write-Output "start lsn: $startLsn"

Ssis 'pull' (@('pull') + $target + @('--out', 'SILVER', '--write-ispac')) | Out-Null

# 1. Package deploy of one edited package.
Edit-Package "$work\SILVER\Stage_Customer.dtsx" "smoke package deploy $(Get-Date -Format o)"
Plan-Apply 'deploy-packages' (@('deploy-packages') + $target + @('SILVER\Stage_Customer.dtsx')) | Out-Null

# 2. Project deploy of a subset from a built .ispac: only Stage_Product changes, others kept.
Edit-Package "$work\SILVER\Stage_Product.dtsx" "smoke project deploy $(Get-Date -Format o)"
Edit-Package "$work\SILVER\Stage_Ledger.dtsx" "must NOT be deployed $(Get-Date -Format o)"
$ispac = Get-ChildItem "$work\SILVER" -Filter *.ispac | Select-Object -First 1 -ExpandProperty FullName
if (-not $ispac) { throw 'pull --write-ispac wrote no .ispac' }
@'
import sys, zipfile
from urllib.parse import quote
src, dst, *pkgs = sys.argv[1:]
repl = {quote(p.split("\\")[-1]): open(p, "rb").read() for p in pkgs}
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        zout.writestr(item, repl.get(item.filename, zin.read(item.filename)))
'@ | Set-Content -Encoding ascii "$work\build_ispac.py"
& $py "$work\build_ispac.py" $ispac "$work\SILVER-built.ispac" "$work\SILVER\Stage_Product.dtsx" "$work\SILVER\Stage_Ledger.dtsx"
if ($LASTEXITCODE -ne 0) { throw 'building the test .ispac failed' }
Plan-Apply 'deploy-project keep' (@('deploy-project') + $target + @(
    '--ispac', "$work\SILVER-built.ispac", '--only', 'Stage_Product.dtsx', '--base-dir', "$work\SILVER")) | Out-Null

# 3. Same, but through the merged full-project deploy (catalog.deploy_project).
Edit-Package "$work\SILVER\Stage_Product.dtsx" "smoke merged deploy $(Get-Date -Format o)"
& $py "$work\build_ispac.py" $ispac "$work\SILVER-built2.ispac" "$work\SILVER\Stage_Product.dtsx" "$work\SILVER\Stage_Ledger.dtsx"
Plan-Apply 'deploy-project update' (@('deploy-project') + $target + @(
    '--ispac', "$work\SILVER-built2.ispac", '--only', 'Stage_Product.dtsx', '--base-dir', "$work\SILVER",
    '--project-items', 'update')) | Out-Null

# 4. No base record (a different machine) and a changed file: the plan must refuse.
Edit-Package "$work\SILVER\Stage_Customer.dtsx" "no base $(Get-Date -Format o)"
Rename-Item $env:SSISBLISS_STATE_DIR "$work\state-backup"   # no record of your version, as on a new machine
Ssis 'deploy-packages without base (expect refusal)' (@('deploy-packages') + $target + @('SILVER\Stage_Customer.dtsx')) @(2) | Out-Null
Rename-Item "$work\state-backup" $env:SSISBLISS_STATE_DIR

# 5. Roll back to the starting version.
if ($startLsn) {
    $rb = Ssis 'rollback plan' (@('rollback') + $target + @('--to-lsn', "$startLsn"))
    if ($rb.plan_id) {
        $res = Ssis 'rollback apply' (@('rollback') + $target + @('--to-lsn', "$startLsn", '--apply', '--plan-id', $rb.plan_id, '--backup-dir', $backups))
        if ($res.status -ne 'verified') { $failures += "rollback status $($res.status)" }
    } else { $failures += 'rollback: no plan_id' }
}
Ssis 'versions after' (@('versions') + $target) | Out-Null

if ($failures) { throw ("SMOKE FAILED: " + ($failures -join '; ')) }
Write-Output 'SMOKE PASSED'
