param(
    [string]$LogDir = "logs/local-weather-paper"
)

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$PidFile = Join-Path (Join-Path $RepoRoot $LogDir) "weather-paper-loop.pid"

if (-not (Test-Path $PidFile)) {
    Write-Host "No PID file found at $PidFile"
    exit 0
}

$pidText = Get-Content $PidFile | Select-Object -First 1
if (-not $pidText) {
    Write-Host "PID file is empty: $PidFile"
    Remove-Item $PidFile -Force
    exit 0
}

$proc = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Host "Process $pidText is not running; removing stale PID file."
    Remove-Item $PidFile -Force
    exit 0
}

Write-Host "Stopping local weather paper loop pid=$pidText"
Stop-Process -Id ([int]$pidText) -Force
Remove-Item $PidFile -Force
Write-Host "Stopped."
