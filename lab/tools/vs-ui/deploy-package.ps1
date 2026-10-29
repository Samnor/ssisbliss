# Visual Studio, driven like a developer: open Corpus.sln, Rebuild, right-click Load_B.dtsx in Solution
# Explorer, Deploy Package, fill in the deployment wizard, Deploy. Runs in labadmin's desktop session
# (lab.py verify), after vs-ui/prepare.py. Writes $Out\result.json (wizard command line and results).
$sln = 'C:\lab\work\vsui\Corpus\Corpus.sln'
$server, $path = 'localhost,14331', '/SSISDB/PKGCMP/Corpus'
$result = @{ steps = @() }
function Step($name, $ok) { $result.steps += @{ step = $name; ok = [bool]$ok }; Log "$name`: $ok"; if (-not $ok) { Shot "failed-$($result.steps.Count)"; throw "step failed: $name" } }
function WizTexts($wiz) { foreach ($e in $wiz.FindAll($Tree::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) { $e.Current.Name } }

try {
    Get-Process devenv -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep 3
    # A Visual Studio that was killed leaves backups, and the next start opens a Recovered Files dialog first.
    Remove-Item -Recurse -Force "$env:LOCALAPPDATA\Microsoft\VisualStudio\BackupFiles\*" -ErrorAction SilentlyContinue
    Start-Process 'C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\devenv.exe' -ArgumentList "`"$sln`""
    # First start for this user: skip sign-in, accept the default theme.
    $skip = FindId 'NotNowLink' -timeout 45
    if ($skip) { Press $skip | Out-Null; PressId 'FinishButton' -timeout 30 | Out-Null }
    $vs = FindLike 'Corpus - Microsoft Visual Studio*' -timeout 180 -type 'Window'
    Step 'Visual Studio opened the solution' $vs
    $dte = $null
    foreach ($i in 1..30) { try { $dte = [Runtime.InteropServices.Marshal]::GetActiveObject('VisualStudio.DTE.17.0'); break } catch { Start-Sleep 5 } }
    Step 'automation interface (DTE)' $dte

    # Rebuild, as from the Build menu.
    $dte.ExecuteCommand('Build.RebuildSolution')
    $deadline = (Get-Date).AddMinutes(5)
    do { Start-Sleep 2 } while ($dte.Solution.SolutionBuild.BuildState -ne 3 -and (Get-Date) -lt $deadline)
    $result.rebuild_failed_projects = $dte.Solution.SolutionBuild.LastBuildInfo
    Step 'Rebuild succeeded' ($dte.Solution.SolutionBuild.BuildState -eq 3 -and $result.rebuild_failed_projects -eq 0)
    Shot 'a-rebuilt'

    # Solution Explorer: select Load_B.dtsx, right-click, Deploy Package.
    $dte.ExecuteCommand('View.SolutionExplorer')
    Expand (FindLike 'Corpus (SQL Server*' $vs 20 'TreeItem') | Out-Null; Start-Sleep 2
    Expand (Find 'SSIS Packages' $vs 10 'TreeItem') | Out-Null; Start-Sleep 2
    $item = Find 'Load_B.dtsx' $vs 10 'TreeItem'
    Step 'Load_B.dtsx selected in Solution Explorer' ($item -and (SelectItem $item))
    RightClick $item; Start-Sleep 2
    $menu = Find 'Deploy Package' -timeout 10 -type 'MenuItem'
    Step 'context menu has Deploy Package' $menu
    $r = $menu.Current.BoundingRectangle
    [Mouse]::Click([int]($r.X + $r.Width / 2), [int]($r.Y + $r.Height / 2))

    # The deployment wizard (WinForms: its buttons are panes; click them where they are).
    $wiz = FindLike '*Deployment Wizard*' -timeout 60 -type 'Window'
    Step 'deployment wizard opened' $wiz
    function ClickNamed($name) {
        $e = Find $name $wiz 10
        if (-not $e) { return $false }
        $b = $e.Current.BoundingRectangle; [Mouse]::Click([int]($b.X + $b.Width / 2), [int]($b.Y + $b.Height / 2)); return $true
    }
    $pages = @()
    foreach ($i in 1..3) {   # Introduction -> (Select Source, skipped when Visual Studio supplies it) -> Target -> Destination
        $title = (WizTexts $wiz | Where-Object { $_ -in 'Introduction', 'Select Source', 'Select Deployment Target', 'Select Destination' } | Select-Object -Last 1)
        $pages += $title
        if ($title -eq 'Select Destination') { break }
        ClickNamed 'Next >' | Out-Null; Start-Sleep 4
    }
    $result.wizard_pages = $pages
    # The text boxes are not exposed to UI Automation: click the box under its label and type.
    function TypeUnder($label, $text) {
        $l = Find $label $wiz 10
        if (-not $l) { return $false }
        $b = $l.Current.BoundingRectangle
        [Mouse]::Click([int]($b.X + 240), [int]($b.Bottom + 12)); Start-Sleep 1
        [System.Windows.Forms.SendKeys]::SendWait('^a' + $text); Start-Sleep 1
        return $true
    }
    Step 'server name entered' (TypeUnder 'Server name:' $server)
    ClickNamed 'Connect' | Out-Null; Start-Sleep 8
    Step 'catalog path entered' (TypeUnder 'Path:' $path)
    Shot 'b-destination'
    ClickNamed 'Next >' | Out-Null; Start-Sleep 5
    $review = @(WizTexts $wiz)
    $result.command_line = ($review | Where-Object { $_ -like 'Command line:*' } | Select-Object -First 1)
    $result.review = $review | Where-Object { $_ -like 'Path:*' -or $_ -like 'Package#*' -or $_ -like 'Server name:*' }
    # The summary tree is not exposed to UI Automation; c-review.png shows the command line Visual Studio passes.
    Shot 'c-review'
    ClickNamed 'Deploy' | Out-Null
    Start-Sleep 25; Shot 'd-results'
    # The results grid is not exposed to UI Automation either (d-results.png shows it); success is judged
    # from what reached the server (vs-ui/capture.py).
    Step 'wizard reached its Results page' (Find 'Save Report...' $wiz 15)
    ClickNamed 'Close' | Out-Null
} finally {
    $result | ConvertTo-Json -Depth 5 | Set-Content "$Out\result.json"
}
