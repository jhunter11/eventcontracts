param(
    [string]$Repo = (Resolve-Path ".").Path,
    [int]$MaxStatus = 80
)

$ErrorActionPreference = "Stop"
Set-Location -Path $Repo

Write-Host "# Eventcontracts Agent Intake"
Write-Host "repo=$Repo"
Write-Host "time=$(Get-Date -Format o)"

Write-Host ""
Write-Host "## Git"
git branch --show-current 2>$null
git status --short 2>$null | Select-Object -First $MaxStatus

Write-Host ""
Write-Host "## Active Safety Constraints"
if (Test-Path "AGENTS.md") {
    Select-String -Path "AGENTS.md" -Pattern "NO TRADING|NO ORDERS|live-submit|authenticated writes|Verify|Prove before expand" | ForEach-Object {
        "{0}:{1}" -f $_.LineNumber, $_.Line.Trim()
    }
} else {
    Write-Host "No repo AGENTS.md found."
}

Write-Host ""
Write-Host "## Tool Versions"
$python = ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
& $python --version 2>$null
git --version 2>$null
cargo --version 2>$null
rustc --version 2>$null

Write-Host ""
Write-Host "## Background Eventcontracts Processes"
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match "eventcontracts|kalshi|kalshi-ws|live-paper|live_paper|ws-capture|tick-log|tick_logger|tick_capture|capture_weather|capture-kalshi" } |
    Select-Object ProcessId, Name, CommandLine |
    Format-Table -Wrap

Write-Host ""
Write-Host "## Process Registry"
if (Test-Path "live-test\process-registry.jsonl") {
    Get-Content "live-test\process-registry.jsonl" -Tail 20
} else {
    Write-Host "No live-test\process-registry.jsonl yet."
}

Write-Host ""
Write-Host "## Recommended Next Gate"
Write-Host "powershell -ExecutionPolicy Bypass -File scripts\verify-eventcontracts.ps1 -ChangedOnly -RunDangerScan"
