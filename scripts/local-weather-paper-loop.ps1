param(
    [int]$IntervalSeconds = 600,
    [string]$Ledger = "data/weather-paper/kxhigh_ledger.jsonl",
    [string]$LogDir = "logs/local-weather-paper",
    [ValidateSet("Normal", "AboveNormal", "High")]
    [string]$Priority = "High",
    [switch]$NoKeepAwake
)

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Missing $Python. Create the repo-root .venv first."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("weather-paper-loop-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")
$PidFile = Join-Path $LogDir "weather-paper-loop.pid"
$PID | Set-Content -Encoding ASCII $PidFile

try {
    (Get-Process -Id $PID).PriorityClass = $Priority
} catch {
    "WARN: could not set priority to $Priority`: $($_.Exception.Message)" | Tee-Object -FilePath $Log -Append
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
        # Non-fatal: the loop can still run while the machine is awake.
    }
}

function Write-LogLine([string]$Text) {
    $line = "[" + (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") + "] " + $Text
    $line | Tee-Object -FilePath $Log -Append
}

Write-LogLine "started pid=$PID repo=$RepoRoot priority=$Priority interval=${IntervalSeconds}s ledger=$Ledger"
Write-LogLine "python=$Python"

while ($true) {
    Keep-Awake
    Write-LogLine "record pass starting"
    & $Python python/scripts/weather_kxhigh_paper.py --record $Ledger 2>&1 |
        Tee-Object -FilePath $Log -Append
    Write-LogLine "record pass exit=$LASTEXITCODE"

    Keep-Awake
    Write-LogLine "settle/enrich pass starting"
    & $Python python/scripts/weather_kxhigh_paper.py --settle $Ledger --write-settled 2>&1 |
        Tee-Object -FilePath $Log -Append
    Write-LogLine "settle/enrich pass exit=$LASTEXITCODE"

    Keep-Awake
    Write-LogLine "sleeping ${IntervalSeconds}s"
    Start-Sleep -Seconds $IntervalSeconds
}
