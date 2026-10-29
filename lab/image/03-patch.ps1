# Apply the target CU/security update to all instances and shared features.
$ErrorActionPreference = 'Stop'

$patch = "C:\lab\media\$($Lab.patch.kb).exe"
$extract = "C:\lab\media\$($Lab.patch.kb)"
if (-not (Test-Path $extract)) {
    # Extract first: running the self-extractor directly under SSM is unreliable.
    $p = Start-Process -FilePath $patch -ArgumentList "/x:$extract", '/q' -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "Extracting $patch failed with exit code $($p.ExitCode)" }
}

$p = Start-Process -FilePath "$extract\setup.exe" -Wait -PassThru -NoNewWindow -ArgumentList @(
    '/quiet', '/IAcceptSQLServerLicenseTerms', '/Action=Patch', '/AllInstances')
Write-Output "Patch exit code: $($p.ExitCode)"
if ($p.ExitCode -notin 0, 3010) {
    Get-Content 'C:\Program Files\Microsoft SQL Server\150\Setup Bootstrap\Log\Summary.txt' -Tail 60 -ErrorAction SilentlyContinue
    throw "Patch failed with exit code $($p.ExitCode)"
}
