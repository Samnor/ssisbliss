# Per-user part of the VS Code agent setup, run as labadmin in the desktop session (lab machine only):
# the Claude Code and Codex extensions (GitHub Copilot Chat is built into VS Code), the ssisbliss skill
# for all three agents, and settings that let the agents run commands without asking, so test runs are
# unattended. Never use these approval settings on a machine with real data.
$code = 'C:\Program Files\Microsoft VS Code\bin\code.cmd'
foreach ($e in 'anthropic.claude-code', 'openai.chatgpt') {
    cmd /c "`"$code`" --install-extension $e 2>&1" | Out-String | Add-Content "$Out\log.txt"
}
cmd /c "`"$code`" --list-extensions --show-versions 2>&1" | Out-String | Add-Content "$Out\log.txt"
cmd /c "C:\lab\tools\venv\Scripts\ssisbliss.exe install-skill --claude --codex --copilot 2>&1" | Out-String | Add-Content "$Out\log.txt"

$settings = "$env:APPDATA\Code\User\settings.json"
New-Item -ItemType Directory -Force (Split-Path $settings) | Out-Null
$s = if (Test-Path $settings) { Get-Content $settings -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
$want = [ordered]@{
    'claudeCode.initialPermissionMode' = 'bypassPermissions'
    'claudeCode.allowDangerouslySkipPermissions' = $true
    'chat.agent.enabled' = $true
    'chat.tools.global.autoApprove' = $true
    'chat.tools.autoApprove' = $true
    'security.workspace.trust.enabled' = $false
    'telemetry.telemetryLevel' = 'off'
}
foreach ($k in $want.Keys) { $s | Add-Member -NotePropertyName $k -NotePropertyValue $want[$k] -Force }
$s | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $settings
"settings: $settings" | Add-Content "$Out\log.txt"

$codex = "$env:USERPROFILE\.codex\config.toml"
New-Item -ItemType Directory -Force (Split-Path $codex) | Out-Null
$toml = if (Test-Path $codex) { Get-Content $codex -Raw } else { '' }
if ($toml -notmatch 'approval_policy') { $toml = "approval_policy = `"never`"`nsandbox_mode = `"danger-full-access`"`n" + $toml }
Set-Content -Encoding utf8 $codex $toml
"codex: $codex" | Add-Content "$Out\log.txt"
Get-ChildItem "$env:USERPROFILE\.claude\skills", "$env:USERPROFILE\.codex\skills", "$env:USERPROFILE\.copilot\skills" -Recurse -Filter SKILL.md |
    ForEach-Object { "skill: $($_.FullName)" } | Add-Content "$Out\log.txt"
