# Lab only: sign labadmin in automatically at boot so a desktop session exists for UI automation.
$ErrorActionPreference = 'Stop'
$pw = (Get-SSMParameter -Name $Lab.rdp_password_parameter -WithDecryption $true -Region $Lab.region).Value
$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty $k AutoAdminLogon '1'
Set-ItemProperty $k DefaultUserName 'labadmin'
Set-ItemProperty $k DefaultDomainName $env:COMPUTERNAME
Set-ItemProperty $k DefaultPassword $pw
Remove-ItemProperty $k AutoLogonCount -ErrorAction SilentlyContinue
# No lock screen / screen saver in that session.
New-Item 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' -Force | Out-Null
Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' NoLockScreen 1 -Type DWord
"autologon set for labadmin"
