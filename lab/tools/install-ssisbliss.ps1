# Install Python and this repo's wheel on the lab host so onprem-ssis can run with Windows auth.
# lab.py install-tools uploads the wheel to the lab bucket and sets $Lab.wheel_key.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$python = 'C:\Python312\python.exe'
if (-not (Test-Path $python)) {
    $installer = 'C:\lab\media\python-3.12.10-amd64.exe'
    if (-not (Test-Path $installer)) {
        Write-Output 'Downloading Python 3.12'
        Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $installer -UseBasicParsing
    }
    $p = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=1', 'TargetDir=C:\Python312', 'PrependPath=1', 'Include_test=0')
    if ($p.ExitCode -ne 0) { throw "Python install failed with exit code $($p.ExitCode)" }
}

$tools = 'C:\lab\tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null
$wheel = Join-Path $tools (Split-Path $Lab.wheel_key -Leaf)
Read-S3Object -BucketName $Lab.bucket -Key $Lab.wheel_key -File $wheel -Region $Lab.region | Out-Null

if (-not (Test-Path "$tools\venv\Scripts\python.exe")) { & $python -m venv "$tools\venv" }
# Same version number on every rebuild: install dependencies, then force the fresh wheel itself.
& "$tools\venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check $wheel
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& "$tools\venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --force-reinstall --no-deps $wheel
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$drivers = (Get-OdbcDriver -Platform '64-bit' | Where-Object Name -like 'ODBC Driver * for SQL Server').Name -join ', '
Write-Output "onprem-ssis installed at $tools\venv\Scripts\onprem-ssis.exe; ODBC drivers: $drivers"
