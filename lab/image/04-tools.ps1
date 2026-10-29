# Client tooling for hands-on work over RDP. Failures here are reported, not fatal.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

try {
    Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force | Out-Null
    Set-PSRepository -Name PSGallery -InstallationPolicy Trusted
    Install-Module -Name SqlServer, dbatools -Scope AllUsers -Force -AllowClobber
    Write-Output 'Installed SqlServer and dbatools modules'
}
catch { Write-Output "WARN modules: $_" }

try {
    # SSMS 21+ ships as a Visual Studio installer bootstrapper; the old /install /quiet
    # switches make it wait on an invisible dialog forever, so guard with a timeout.
    $ssms = 'C:\lab\media\vs_SSMS.exe'
    if (-not (Test-Path $ssms)) { Invoke-WebRequest -Uri 'https://aka.ms/ssms/21/release/vs_SSMS.exe' -OutFile $ssms -UseBasicParsing }
    $p = Start-Process -FilePath $ssms -ArgumentList '--quiet', '--norestart', '--wait' -PassThru
    if (-not $p.WaitForExit(45 * 60 * 1000)) {
        Get-Process vs_*, setup -ErrorAction SilentlyContinue | Stop-Process -Force
        Write-Output 'WARN SSMS: installer did not finish in 45 minutes, stopped'
    }
    else { Write-Output "SSMS installer exit code: $($p.ExitCode)" }
}
catch { Write-Output "WARN SSMS: $_" }
