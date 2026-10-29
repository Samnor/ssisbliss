# VS Code (system install) and Git for Windows, and ssisbliss on the system PATH, so agents in VS Code
# (GitHub Copilot, Claude Code, Codex) can be tested with the ssisbliss skill. Idempotent.
# Extensions, skills and settings are per user: lab/tools/vscode/setup-user.ps1 runs as labadmin.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$media = 'C:\lab\media'
New-Item -ItemType Directory -Force -Path $media | Out-Null

$code = 'C:\Program Files\Microsoft VS Code\bin\code.cmd'
if (-not (Test-Path $code)) {
    $setup = "$media\VSCodeSetup-x64.exe"
    Invoke-WebRequest 'https://update.code.visualstudio.com/latest/win32-x64/stable' -OutFile $setup -UseBasicParsing
    $p = Start-Process $setup -Wait -PassThru -ArgumentList '/VERYSILENT', '/NORESTART', '/MERGETASKS=!runcode,addtopath'
    if ($p.ExitCode -ne 0) { throw "VS Code install failed with exit code $($p.ExitCode)" }
}
Write-Output "VS Code: $((& $code --version | Select-Object -First 1))"

$git = 'C:\Program Files\Git\cmd\git.exe'
if (-not (Test-Path $git)) {
    $rel = Invoke-RestMethod 'https://api.github.com/repos/git-for-windows/git/releases/latest' -UseBasicParsing
    $asset = $rel.assets | Where-Object name -match '^Git-[\d.]+-64-bit\.exe$' | Select-Object -First 1
    $setup = "$media\$($asset.name)"
    Invoke-WebRequest $asset.browser_download_url -OutFile $setup -UseBasicParsing
    $p = Start-Process $setup -Wait -PassThru -ArgumentList '/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-'
    if ($p.ExitCode -ne 0) { throw "Git install failed with exit code $($p.ExitCode)" }
}
Write-Output "Git: $((& $git --version))"

# ssisbliss (installed by lab.py install-tools into C:\lab\tools\venv) on the machine PATH.
$machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
foreach ($dir in 'C:\lab\tools\venv\Scripts', 'C:\Program Files\Git\cmd') {
    if ($machine -notlike "*$dir*") { $machine = "$machine;$dir" }
}
[Environment]::SetEnvironmentVariable('Path', $machine, 'Machine')
Write-Output 'PATH: ssisbliss and git added for all users'
