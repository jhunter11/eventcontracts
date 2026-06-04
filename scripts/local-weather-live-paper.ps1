param(
    [string]$Out = "data/live-paper-tick",
    [int]$RediscoverIntervalSeconds = 30,
    [int]$ForecastIntervalSeconds = 60,
    [int]$SnapshotIntervalSeconds = 15,
    [int]$MaxDurationSeconds = 86400,
    [int]$DiscoverMaxPages = 5,
    [string]$Patterns = "KXHIGH*",
    [string]$SeriesTickers = "KXHIGHNY,KXHIGHCHI,KXHIGHMIA",
    [ValidateSet("account", "sleeve")]
    [string]$CashSource = "sleeve",
    [string]$LogDir = "logs/local-weather-live-paper",
    [ValidateSet("Normal", "AboveNormal", "High")]
    [string]$Priority = "High",
    [switch]$NoKeepAwake,
    [switch]$NoRestart
)

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $RepoRoot

$Exe = Join-Path $RepoRoot ".venv\Scripts\eventcontracts.exe"
if (-not (Test-Path $Exe)) {
    throw "Missing $Exe. Create the repo-root .venv first."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("live-paper-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
$PidFile = Join-Path $LogDir "weather-live-paper.pid"
$PID | Set-Content -Encoding ASCII $PidFile

try {
    (Get-Process -Id $PID).PriorityClass = $Priority
} catch {
    "WARN: could not set supervisor priority to $Priority`: $($_.Exception.Message)" | Tee-Object -FilePath $Log -Append
}

if (-not $NoKeepAwake) {
    try {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class SleepUtil {
  [DllImport("kernel32.dll")]
  public static extern uint SetThreadExecutionState(uint esFlags);
}
"@
    } catch {
        "WARN: could not load keep-awake helper: $($_.Exception.Message)" | Tee-Object -FilePath $Log -Append
    }
}

function Keep-Awake {
    if ($NoKeepAwake) { return }
    try {
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        [SleepUtil]::SetThreadExecutionState(0x80000000 -bor 0x00000001 -bor 0x00000040) | Out-Null
    } catch {
    }
}

function Write-LogLine([string]$Text) {
    $line = "[" + (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") + "] " + $Text
    $line | Tee-Object -FilePath $Log -Append
}

Write-LogLine "started supervisor pid=$PID repo=$RepoRoot priority=$Priority"
Write-LogLine "mode=dry-run live-paper no venue orders are submitted"
Write-LogLine "patterns=$Patterns series=$SeriesTickers cash_source=$CashSource"

$argsList = @(
    "live-paper",
    "--strategy", "configs/strategies/weather-temperature-arbitrage.toml",
    "--sleeve", "configs/sleeves/weather-kalshi-paper-a.toml",
    "--out", $Out,
    "--patterns", $Patterns,
    "--series-tickers", $SeriesTickers,
    "--cash-source", $CashSource,
    "--rediscover-interval-seconds", "$RediscoverIntervalSeconds",
    "--forecast-interval-seconds", "$ForecastIntervalSeconds",
    "--snapshot-interval-seconds", "$SnapshotIntervalSeconds",
    "--max-duration-seconds", "$MaxDurationSeconds",
    "--discover-timeout-seconds", "45",
    "--discover-max-pages", "$DiscoverMaxPages"
)

do {
    Keep-Awake
    Write-LogLine "launching eventcontracts $($argsList -join ' ')"
    & $Exe @argsList 2>&1 | Tee-Object -FilePath $Log -Append
    $exitCode = $LASTEXITCODE
    Write-LogLine "live-paper exited code=$exitCode"
    if ($NoRestart) {
        break
    }
    Write-LogLine "restarting in 10s"
    Start-Sleep -Seconds 10
} while ($true)
