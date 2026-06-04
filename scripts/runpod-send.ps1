param(
    [string]$Code = ("eventcontracts-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")),
    [string]$Archive = "",
    [switch]$NoSend,
    [switch]$IncludeEnv,
    [switch]$NoWeatherData
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir "..")
$TmpDir = Join-Path $Root ".tmp-runpod"
if ([string]::IsNullOrWhiteSpace($Archive)) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $Archive = Join-Path $TmpDir "eventcontracts-runpod-$stamp.tar.gz"
}

Set-Location $Root
New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null
$Stage = Join-Path $TmpDir "stage"
$Manifest = Join-Path $TmpDir "manifest.txt"
if (Test-Path $Stage) {
    Remove-Item -Recurse -Force $Stage
}
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "eventcontracts") | Out-Null

$files = @(& git ls-files --cached --others --exclude-standard)

if (-not $NoWeatherData) {
    foreach ($path in @("configs/weather", "data/weather-calib", "data/weather-paper")) {
        if (Test-Path $path) {
            $files += Get-ChildItem -File -Recurse $path | ForEach-Object {
                Resolve-Path -Relative $_.FullName
            }
        }
    }
}

if ($IncludeEnv -and (Test-Path ".env")) {
    $files += ".env"
}

$files = $files |
    Where-Object { $_ -and (Test-Path $_ -PathType Leaf) } |
    ForEach-Object { ($_ -replace "^[.][\\/]", "") -replace "\\", "/" } |
    Sort-Object -Unique

$files | Set-Content -Encoding UTF8 $Manifest

foreach ($rel in $files) {
    $src = Join-Path $Root $rel
    $dst = Join-Path (Join-Path $Stage "eventcontracts") $rel
    $dstDir = Split-Path -Parent $dst
    New-Item -ItemType Directory -Force -Path $dstDir | Out-Null
    Copy-Item -LiteralPath $src -Destination $dst -Force
}

tar -czf $Archive -C $Stage eventcontracts

$archiveName = Split-Path -Leaf $Archive
Write-Host ""
Write-Host "Created archive:"
Write-Host "  $Archive"
Write-Host ""
Write-Host "On the RunPod, open a terminal and run:"
Write-Host ""
Write-Host "  cd /workspace"
Write-Host "  runpodctl receive $Code"
Write-Host "  tar -xzf $archiveName -C /workspace"
Write-Host "  cd /workspace/eventcontracts"
Write-Host "  bash scripts/runpod-background.sh"
Write-Host "  tail -f logs/runpod/run-*.log"
Write-Host ""

if (-not $NoSend) {
    $cmd = Get-Command runpodctl -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "runpodctl not found. Install it first: https://docs.runpod.io/runpodctl/overview"
    }
    & runpodctl send $Archive --code $Code
}
