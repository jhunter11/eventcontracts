param(
    [int]$IntervalSeconds = 600,
    [string]$Ledger = "data/weather-paper/kxhigh_ledger.jsonl",
    [string]$LogDir = "logs/local-weather-paper",
    [ValidateSet("Normal", "AboveNormal", "High")]
    [string]$Priority = "High",
    [switch]$NoKeepAwake,
    [switch]$Visible
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$LoopScript = Join-Path $ScriptDir "local-weather-paper-loop.ps1"
$LogPath = Join-Path $RepoRoot $LogDir
$PidFile = Join-Path $LogPath "weather-paper-loop.pid"
New-Item -ItemType Directory -Force -Path $LogPath | Out-Null

if (Test-Path $PidFile) {
    $oldPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($oldPid) {
        $oldProc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($oldProc) {
            Write-Host "Already running: pid=$oldPid"
            Write-Host "Stop it with: powershell -ExecutionPolicy Bypass -File scripts\stop-local-weather-paper-loop.ps1"
            exit 0
        }
    }
}

$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$argsList = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$LoopScript`"",
    "-IntervalSeconds", "$IntervalSeconds",
    "-Ledger", "`"$Ledger`"",
    "-LogDir", "`"$LogDir`"",
    "-Priority", "$Priority"
)
if ($NoKeepAwake) {
    $argsList += "-NoKeepAwake"
}

$windowStyle = if ($Visible) { "Normal" } else { "Hidden" }
$proc = Start-Process -FilePath $PowerShell `
    -ArgumentList $argsList `
    -WorkingDirectory $RepoRoot `
    -WindowStyle $windowStyle `
    -PassThru

Start-Sleep -Seconds 2
if (Test-Path $PidFile) {
    $loopPid = Get-Content $PidFile | Select-Object -First 1
} else {
    $loopPid = $proc.Id
}

Write-Host "Started local weather paper loop."
Write-Host "launcher_pid=$($proc.Id) loop_pid=$loopPid"
Write-Host "log_dir=$LogPath"
Write-Host "tail logs with:"
Write-Host "  Get-Content $LogDir\weather-paper-loop-*.log -Wait -Tail 60"
