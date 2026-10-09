# Report images for the docs: render each page in its own Power BI window (powerbi\capture_pages.ps1, never a window the
# user has open), then copy the canvas captures to docs\img in reading order. With -NoCapture, copy the latest captures.
param([switch]$NoCapture)
$root = Split-Path $PSScriptRoot -Parent
$caps = "$root\powerbi\.captures"
$img = "$root\docs\img"
New-Item -ItemType Directory -Force $img | Out-Null
if (-not $NoCapture) { & "$root\powerbi\capture_pages.ps1" -NoBuild; if ($LASTEXITCODE) { "capture failed"; exit 1 } }
$pages = @(@("bad", "01-bad"), @("when", "02-when"), @("spikes", "03-spikes"), @("hours", "04-hours"), @("trust", "05-trust"))
foreach ($p in $pages) {
    $src = "$caps\canvas-$($p[0]).png"
    if (-not (Test-Path $src)) { "missing $src"; exit 1 }
    Copy-Item $src "$img\$($p[1]).png" -Force
}
"copied $($pages.Count) report images to docs\img"
exit 0
