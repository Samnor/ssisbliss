# Visual Studio 2022 Community with the SSIS Projects extension, the standalone SSIS DevOps tools
# (SSISBuild.exe, SSISDeploy.exe) and an RDP login, so Microsoft's own deploy paths can be compared
# with onprem-ssis. Idempotent; each part is skipped when already present.
# VS Community licence: fine for this open-source lab; check the terms before other use.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$media = 'C:\lab\media'
New-Item -ItemType Directory -Force -Path $media, 'C:\lab\tools' | Out-Null

function Get-VsPath {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) { & $vswhere -version '[17.0,18.0)' -products * -property installationPath | Select-Object -First 1 }
}

# 1. Visual Studio 2022 Community (core editor + data workload).
$vs = Get-VsPath
if (-not $vs) {
    $boot = "$media\vs_community.exe"
    if (-not (Test-Path $boot)) { Invoke-WebRequest 'https://aka.ms/vs/17/release/vs_community.exe' -OutFile $boot -UseBasicParsing }
    Write-Output 'Installing Visual Studio 2022 Community (30-60 minutes)'
    $p = Start-Process $boot -Wait -PassThru -ArgumentList @(
        '--quiet', '--wait', '--norestart', '--nocache', '--add', 'Microsoft.VisualStudio.Workload.Data', '--includeRecommended')
    if ($p.ExitCode -notin 0, 3010) { throw "Visual Studio install failed with exit code $($p.ExitCode)" }
    $vs = Get-VsPath
    if (-not $vs) { throw 'Visual Studio installed but vswhere does not find it' }
}
Write-Output "Visual Studio: $vs"

# 2. SQL Server Integration Services Projects extension (Burn bundle from the Marketplace).
# The bundle installs per machine into CommonExtensions (checked with extension 2.2).
$ssisDir = "$vs\Common7\IDE\CommonExtensions\Microsoft\SSIS"
if (-not (Test-Path $ssisDir)) {
    $ext = "$media\Microsoft.DataTools.IntegrationServices.exe"
    if (-not (Test-Path $ext)) {
        Invoke-WebRequest 'https://marketplace.visualstudio.com/_apis/public/gallery/publishers/SSIS/vsextensions/MicrosoftDataToolsIntegrationServices/latest/vspackage' `
            -OutFile $ext -UseBasicParsing
    }
    Write-Output 'Installing SSIS Projects extension'
    $log = "$media\ssis-extension-install.log"
    $p = Start-Process $ext -Wait -PassThru -ArgumentList @('/install', '/quiet', '/norestart', '/log', $log)
    Write-Output "SSIS extension installer exit code $($p.ExitCode); log $log"
    if (-not (Test-Path $ssisDir)) {
        Get-Content $log -Tail 40 -ErrorAction SilentlyContinue
        throw 'SSIS Projects extension not found after install (see log above)'
    }
}
Write-Output "SSIS extension: $ssisDir"

# 3. Standalone SSIS DevOps tools.
$devops = 'C:\lab\tools\ssisdevops'
if (-not (Test-Path "$devops\SSISDeploy.exe")) {
    $exe = "$media\SSISDevOpsTools.exe"
    if (-not (Test-Path $exe)) { Invoke-WebRequest 'https://aka.ms/SSISDevOpsTools' -OutFile $exe -UseBasicParsing }
    $p = Start-Process $exe -Wait -PassThru -ArgumentList @('/Q', '/C', "/T:$devops")
    if (-not (Test-Path "$devops\SSISDeploy.exe")) { throw "SSIS DevOps tools not extracted (exit $($p.ExitCode))" }
}
Write-Output "SSIS DevOps tools: $devops"

$wizard = Get-ChildItem 'C:\Program Files\Microsoft SQL Server' -Recurse -Filter ISDeploymentWizard.exe -ErrorAction SilentlyContinue |
    Where-Object FullName -notlike '*x86*' | Select-Object -First 1 -ExpandProperty FullName
Write-Output "ISDeploymentWizard: $wizard"
Set-Content 'C:\lab\tools\ide-installed.txt' "vs=$vs`nssisdevops=$devops`nwizard=$wizard"
