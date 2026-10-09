# Render every page in its own Power BI window (open_pbip.ps1, one window at a time, each closed after its capture)
# and trim each capture to the canvas: .captures\canvas-<page>.png. Builds once first unless -NoBuild.
param([switch]$NoBuild, [string[]]$Pages = @("bad", "when", "spikes", "hours", "trust"), [string[]]$BuildArgs = @())
$caps = "$PSScriptRoot\.captures"
if (-not $NoBuild) {
    & "E:\Claude\ClaudeSpace\venvs\india-air-quality\Scripts\python.exe" "$PSScriptRoot\build_pbip.py" @BuildArgs
    if ($LASTEXITCODE) { "build failed"; exit 1 }
    & "$PSScriptRoot\validate_tmdl.ps1"; if ($LASTEXITCODE) { exit 1 }
}
foreach ($pg in $Pages) {
    & "$PSScriptRoot\open_pbip.ps1" -NoBuild -Page $pg
    if ($LASTEXITCODE) { "capture of $pg failed"; exit 1 }
    & "$PSScriptRoot\trim_canvas.ps1" -In "$caps\pbi-$pg.png" -Out "$caps\canvas-$pg.png"
}
exit 0
