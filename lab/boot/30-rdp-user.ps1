# If the IDE tools are installed (lab.py install-ide), make sure the RDP login exists with the
# password from Parameter Store and that Remote Desktop is enabled. Hosts without them skip this.
$ErrorActionPreference = 'Stop'
if (-not (Test-Path 'C:\lab\tools\ide-installed.txt')) { Write-Output 'no IDE tools; RDP login not needed'; return }

$pw = ConvertTo-SecureString (Get-SSMParameter -Name $Lab.rdp_password_parameter -WithDecryption $true -Region $Lab.region).Value -AsPlainText -Force
if (Get-LocalUser labadmin -ErrorAction SilentlyContinue) { Set-LocalUser labadmin -Password $pw }
else { New-LocalUser labadmin -Password $pw -PasswordNeverExpires -FullName 'Lab administrator' | Out-Null }
if (-not (Get-LocalGroupMember Administrators | Where-Object Name -like '*\labadmin')) { Add-LocalGroupMember Administrators labadmin }
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue
Write-Output 'RDP login labadmin ready (port 3389 reachable only through lab.py tunnel --rdp)'
