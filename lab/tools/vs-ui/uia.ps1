# UI Automation helpers for scripts running in labadmin's desktop session (lab.py run_in_desktop).
# Each script gets $Out for its log and screenshots.
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms, System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices;
public static class Mouse {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(int f, int x, int y, int d, int e);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  public static void Click(int x, int y) { SetCursorPos(x, y); mouse_event(2, 0, 0, 0, 0); mouse_event(4, 0, 0, 0, 0); }
  public static void RightClick(int x, int y) { SetCursorPos(x, y); mouse_event(8, 0, 0, 0, 0); mouse_event(16, 0, 0, 0, 0); }
}
"@
$UIA = [System.Windows.Automation.AutomationElement]
$Tree = [System.Windows.Automation.TreeScope]
function Log($m) { "$(Get-Date -Format HH:mm:ss) $m" | Add-Content "$Out\log.txt" }
function Shot([string]$name) {
    $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
    $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
    $bmp.Save("$Out\$name.png"); $g.Dispose(); $bmp.Dispose()
}
function Find([string]$name, $root = $UIA::RootElement, [int]$timeout = 30, [string]$type = $null) {
    $cond = New-Object System.Windows.Automation.PropertyCondition($UIA::NameProperty, $name)
    $deadline = (Get-Date).AddSeconds($timeout)
    do {
        $all = $root.FindAll($Tree::Descendants, $cond)
        foreach ($e in $all) {
            if (-not $type -or $e.Current.ControlType.ProgrammaticName -eq "ControlType.$type") { return $e }
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $null
}
function Press($e) {
    if (-not $e) { return $false }
    try { $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); return $true } catch { }
    $r = $e.Current.BoundingRectangle
    [Mouse]::Click([int]($r.X + $r.Width / 2), [int]($r.Y + $r.Height / 2)); return $true
}
function PressName([string]$name, $root = $UIA::RootElement, [int]$timeout = 30, [string]$type = $null) {
    $e = Find $name $root $timeout $type
    $ok = Press $e
    Log "press '$name': $ok"
    return $ok
}
function SetText($e, [string]$text) {
    try { $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($text); return $true } catch { Log "SetValue failed: $_"; return $false }
}
function Windows { $UIA::RootElement.FindAll($Tree::Children, [System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object { "$($_.Current.Name) [$($_.Current.ClassName)]" } }
function Dump($root, [int]$depth = 3, [string]$indent = '') {
    if ($depth -lt 0) { return }
    foreach ($c in $root.FindAll($Tree::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
        "$indent$($c.Current.ControlType.ProgrammaticName.Replace('ControlType.','')) '$($c.Current.Name)' id=$($c.Current.AutomationId)"
        Dump $c ($depth - 1) "$indent  "
    }
}
function FindId([string]$id, $root = $UIA::RootElement, [int]$timeout = 30) {
    $cond = New-Object System.Windows.Automation.PropertyCondition($UIA::AutomationIdProperty, $id)
    $deadline = (Get-Date).AddSeconds($timeout)
    do { $e = $root.FindFirst($Tree::Descendants, $cond); if ($e) { return $e }; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline)
    return $null
}
function PressId([string]$id, $root = $UIA::RootElement, [int]$timeout = 30) { $ok = Press (FindId $id $root $timeout); Log "press #$id`: $ok"; return $ok }
function Expand($e) { try { $e.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand(); return $true } catch { return $false } }
function SelectItem($e) { try { $e.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select(); return $true } catch { return $false } }
function RightClick($e) { $r = $e.Current.BoundingRectangle; [Mouse]::RightClick([int]($r.X + $r.Width / 2), [int]($r.Y + $r.Height / 2)) }
function FindLike([string]$pattern, $root = $UIA::RootElement, [int]$timeout = 30, [string]$type = $null) {
    $deadline = (Get-Date).AddSeconds($timeout)
    do {
        foreach ($e in $root.FindAll($Tree::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
            if ($e.Current.Name -like $pattern -and (-not $type -or $e.Current.ControlType.ProgrammaticName -eq "ControlType.$type")) { return $e }
        }
        Start-Sleep -Milliseconds 700
    } while ((Get-Date) -lt $deadline)
    return $null
}
