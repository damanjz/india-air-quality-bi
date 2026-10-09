# Trim a Power BI window capture to the report canvas. The canvas is found by its rail colour (the question rail runs
# down the left edge of every page, top to bottom), so no pixel coordinates are hard-coded; the 16:9 page sets the
# width from the height.
param([Parameter(Mandatory)][string]$In, [Parameter(Mandatory)][string]$Out)
Add-Type -AssemblyName System.Drawing
$bmp = [System.Drawing.Bitmap]::FromFile($In)
function IsRail($c) { [Math]::Abs($c.R - 0xf2) -le 2 -and [Math]::Abs($c.G - 0xef) -le 2 -and [Math]::Abs($c.B - 0xe7) -le 2 }
$midY = [int]($bmp.Height * 0.62)
$left = -1
for ($x = 20; $x -lt $bmp.Width / 2; $x++) { if (IsRail $bmp.GetPixel($x, $midY)) { $left = $x; break } }
if ($left -lt 0) { "canvas not found"; exit 1 }
$probe = $left + 2
$top = $midY; while ($top -gt 0 -and (IsRail $bmp.GetPixel($probe, $top - 1))) { $top-- }
$bottom = $midY; while ($bottom -lt $bmp.Height - 1 -and (IsRail $bmp.GetPixel($probe, $bottom + 1))) { $bottom++ }
$height = $bottom - $top + 1
$width = [int][Math]::Round($height * 1280 / 720)
$canvas = $bmp.Clone((New-Object System.Drawing.Rectangle $left, $top, $width, $height), $bmp.PixelFormat)
$bmp.Dispose()
$canvas.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
"saved $Out ($width x $height)"
exit 0
