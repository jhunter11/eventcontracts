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
    [switch]$NoRestart,
    [switch]$Visible
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$LiveScript = Join-Path $ScriptDir "local-weather-live-paper.ps1"
$LogPath = Join-Path $RepoRoot $LogDir
$PidFile = Join-Path $LogPath "weather-live-paper.pid"
New-Item -ItemType Directory -Force -Path $LogPath | Out-Null

if (Test-Path $PidFile) {
    $oldPid = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid) {
        $oldProc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($oldProc) {
            Write-Host "Already running: pid=$oldPid"
            Write-Host "Stop it with: powershell -ExecutionPolicy Bypass -File scripts\stop-local-weather-live-paper.ps1"
            exit 0
        }
    }
}

$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$argsList = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$LiveScript`"",
    "-Out", "`"$Out`"",
    "-RediscoverIntervalSeconds", "$RediscoverIntervalSeconds",
    "-ForecastIntervalSeconds", "$ForecastIntervalSeconds",
    "-SnapshotIntervalSeconds", "$SnapshotIntervalSeconds",
    "-MaxDurationSeconds", "$MaxDurationSeconds",
    "-DiscoverMaxPages", "$DiscoverMaxPages",
    "-Patterns", "`"$Patterns`"",
    "-SeriesTickers", "`"$SeriesTickers`"",
    "-CashSource", "$CashSource",
    "-LogDir", "`"$LogDir`"",
    "-Priority", "$Priority"
)
if ($NoKeepAwake) {
    $argsList += "-NoKeepAwake"
}
if ($NoRestart) {
    $argsList += "-NoRestart"
}

$windowStyle = if ($Visible) { "Normal" } else { "Hidden" }
$proc = Start-Process -FilePath $PowerShell `
    -ArgumentList $argsList `
    -WorkingDirectory $RepoRoot `
    -WindowStyle $windowStyle `
    -PassThru

Start-Sleep -Seconds 3
if (Test-Path $PidFile) {
    $livePid = Get-Content $PidFile | Select-Object -First 1
} else {
    $livePid = $proc.Id
}

Write-Host "Started local weather live-paper."
Write-Host "launcher_pid=$($proc.Id) supervisor_pid=$livePid"
Write-Host "log_dir=$LogPath"
Write-Host "tail logs with:"
Write-Host "  Get-Content $LogDir\live-paper-*.log -Wait -Tail 80"
