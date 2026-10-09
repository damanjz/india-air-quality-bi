# Print docs/<name>.html to PDF with headless Chromium (no headers or footers). The page and its images are copied to a
# short temporary folder first (long file:/// paths have failed to print) and the folder is removed afterwards.
# With -Review, also render every page to .captures/pdf/<name>-pN.png with Windows' own PDF renderer, so the layout can
# be checked page by page.
param([string]$Name = "case-study", [switch]$Review)
$docs = $PSScriptRoot; $root = Split-Path $docs -Parent
$pdf = Join-Path $docs "$Name.pdf"
$chrome = "E:\Claude\ClaudeSpace\playwright-browsers\chromium_headless_shell-1234\chrome-headless-shell-win64\chrome-headless-shell.exe"
$tmp = "E:\Claude\ClaudeSpace\shots\tmp-aq"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$tmp\img" | Out-Null
Copy-Item (Join-Path $docs "$Name.html") "$tmp\page.html"
Copy-Item (Join-Path $docs "img\*") "$tmp\img"
$profile = "$tmp\profile"
Start-Process $chrome -ArgumentList "--headless", "--disable-gpu", "--no-pdf-header-footer", "--user-data-dir=`"$profile`"",
    "--print-to-pdf=`"$tmp\out.pdf`"", "file:///$($tmp.Replace('\', '/'))/page.html" -Wait -WindowStyle Hidden
if (-not (Test-Path "$tmp\out.pdf")) { "print failed"; exit 1 }
Copy-Item "$tmp\out.pdf" $pdf -Force
Remove-Item -Recurse -Force $tmp
if (-not $Review) { "${pdf}: $([math]::Round((Get-Item $pdf).Length / 1KB)) KB"; exit 0 }

Add-Type -AssemblyName System.Drawing, System.Runtime.WindowsRuntime
$null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$ext = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 }
$asOp = ($ext | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asAct = ($ext | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
function Await($op, $type) { $t = $asOp.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(); $t.Result }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($pdf)) ([Windows.Storage.StorageFile])
$doc = Await ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
$out = Join-Path $root ".captures\pdf"; New-Item -ItemType Directory -Force $out | Out-Null
Get-ChildItem $out -Filter "$Name-p*.png" | Remove-Item
for ($i = 0; $i -lt $doc.PageCount; $i++) {
    $page = $doc.GetPage($i); $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
    $opt = New-Object Windows.Data.Pdf.PdfPageRenderOptions; $opt.DestinationWidth = 900
    $asAct.Invoke($null, @($page.RenderToStreamAsync($stream, $opt))).Wait()
    $bmp = [System.Drawing.Bitmap]::FromStream([System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream))
    $bmp.Save((Join-Path $out "$Name-p$($i + 1).png"), [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose(); $page.Dispose()
}
"${pdf}: $($doc.PageCount) pages, $([math]::Round((Get-Item $pdf).Length / 1KB)) KB"
