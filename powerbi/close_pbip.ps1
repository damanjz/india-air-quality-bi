# Close the Power BI window that open_pbip.ps1 -Keep left open (its pid is in .captures\pbi-pid.txt), the same way
# open_pbip.ps1 closes its own window: its engine and its workspace folder go with it. Other Power BI windows are never touched.
param([int]$ProcessId)
$caps = "$PSScriptRoot\.captures"
if (-not $ProcessId) { $ProcessId = [int](Get-Content "$caps\pbi-pid.txt").Trim() }
$proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
if (-not $proc -or $proc.ProcessName -ne "PBIDesktop") { "no Power BI window with pid $ProcessId"; exit 0 }
$children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($proc.Id)")
$workspaces = @($children | Where-Object { $_.Name -eq 'msmdsrv.exe' } | ForEach-Object {
    if ($_.CommandLine -match '-s "([^"]+)\\Data"') { $Matches[1] } })
$children | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
$proc.WaitForExit(30000) | Out-Null
Start-Sleep -Seconds 3
foreach ($w in $workspaces) {
    if ($w -like "$env:LOCALAPPDATA\Microsoft\Power BI Desktop\AnalysisServicesWorkspaces\AnalysisServicesWorkspace_*") {
        Remove-Item -LiteralPath $w -Recurse -Force -ErrorAction SilentlyContinue
    }
}
Remove-Item "$caps\pbi-pid.txt", "$caps\pbi-port.txt" -ErrorAction SilentlyContinue
"closed our window (pid $ProcessId)"
exit 0
