param(
    [string]$LogDir = "logs/local-weather-live-paper"
)

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$PidFile = Join-Path (Join-Path $RepoRoot $LogDir) "weather-live-paper.pid"

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

function Stop-ProcessTree([int]$ProcessId) {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
    }
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

$proc = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Host "Process $pidText is not running; removing stale PID file."
    Remove-Item $PidFile -Force
    exit 0
}

Write-Host "Stopping local weather live-paper supervisor pid=$pidText"
Stop-ProcessTree -ProcessId ([int]$pidText)
Remove-Item $PidFile -Force
Write-Host "Stopped."
