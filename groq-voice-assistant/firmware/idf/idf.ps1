# Windows: run idf.py for this project with an installed ESP-IDF v5.5.
# Examples:  .\idf.ps1 build        .\idf.ps1 -p COM6 flash monitor
# Uses $env:IDF_PATH if set, otherwise looks in the usual install folders.
$candidates = @()
if ($env:IDF_PATH) { $candidates += $env:IDF_PATH }
foreach ($root in 'C:\Espressif', 'D:\Espressif', "$env:USERPROFILE\esp", "$env:USERPROFILE\.espressif") {
    $candidates += Get-ChildItem -Path "$root\frameworks", $root -Directory -Filter 'esp-idf*' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'v?5\.5|^esp-idf$' } | Sort-Object Name -Descending | ForEach-Object FullName
}
$idf = $candidates | Where-Object { $_ -and (Test-Path (Join-Path $_ 'export.ps1')) } | Select-Object -First 1
if (-not $idf) {
    Write-Host 'ESP-IDF v5.5 not found. Install it (https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/get-started/windows-setup.html)'
    Write-Host 'or set IDF_PATH to its folder, then run this again.'
    exit 1
}
$env:IDF_PATH = $idf
# The Espressif installer keeps the matching tools two folders up (e.g. C:\Espressif).
# Prefer those over a global IDF_TOOLS_PATH, which may belong to an older ESP-IDF.
$toolsRoot = Split-Path (Split-Path $idf -Parent) -Parent
if (Test-Path (Join-Path $toolsRoot 'tools')) { $env:IDF_TOOLS_PATH = $toolsRoot }
. "$idf\export.ps1" *> $null
Set-Location $PSScriptRoot
idf.py @args
exit $LASTEXITCODE
