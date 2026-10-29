# Download SQL Server Developer media and the target patch to C:\lab\media.
# lab.py prepends $Lab (the versions.json entry plus runtime settings).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$media = 'C:\lab\media'
New-Item -ItemType Directory -Force -Path $media | Out-Null

$ssei = Join-Path $media 'SSEI-Dev.exe'
if (-not (Test-Path $ssei)) {
    Write-Output "Downloading setup bootstrapper"
    Invoke-WebRequest -Uri $Lab.setup_bootstrap_url -OutFile $ssei -UseBasicParsing
}

if (-not (Get-ChildItem $media -Filter *.iso -ErrorAction SilentlyContinue)) {
    Write-Output "Downloading Developer edition ISO (several GB)"
    $p = Start-Process -FilePath $ssei -Wait -PassThru -ArgumentList @(
        '/Action=Download', "/MediaPath=$media", '/MediaType=ISO', '/Language=en-US', '/Quiet')
    if ($p.ExitCode -ne 0) { throw "SSEI download failed with exit code $($p.ExitCode)" }
}

$patch = Join-Path $media "$($Lab.patch.kb).exe"
if (-not (Test-Path $patch)) {
    Write-Output "Downloading $($Lab.patch.kb)"
    Invoke-WebRequest -Uri $Lab.patch.url -OutFile $patch -UseBasicParsing
}

Get-ChildItem $media | Format-Table Name, @{n = 'MB'; e = { [int]($_.Length / 1MB) } } -AutoSize | Out-String
