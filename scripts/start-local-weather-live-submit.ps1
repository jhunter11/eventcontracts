param(
    [ValidateSet("paper", "live")]
    [string]$Mode = "paper",
    [string]$SignalFile = "",
    [string]$Pattern = "KXHIGH",
    [string]$Tickers = "",
    [int]$MaxMarkets = 36,
    [int]$DurationSeconds = 3600,
    [int]$MaxLiveOrders = 1,
    [string]$StrategySpec = "configs/strategies/weather-temperature-arbitrage-live.toml",
    [string]$SleeveSpec = "configs/sleeves/weather-kalshi-live-a.toml",
    [string]$LogDir = "logs/local-weather-live-submit",
    [switch]$CancelOrphans,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $RepoRoot

if ([string]::IsNullOrWhiteSpace($SignalFile)) {
    $latestRun = Get-ChildItem -Path "data/live-paper-tick" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if ($null -eq $latestRun) {
        throw "No data/live-paper-tick run directory found. Start scripts\start-local-weather-live-paper.ps1 first."
    }
    $SignalFile = Join-Path $latestRun.FullName "external_signals.jsonl"
}

$SignalFile = (Resolve-Path $SignalFile).Path
if (-not (Test-Path $SignalFile)) {
    throw "Signal file not found: $SignalFile"
}

if ([string]::IsNullOrWhiteSpace($Tickers)) {
    $derivedTickers = Get-Content $SignalFile -Tail 1000 |
        ForEach-Object {
            if (-not [string]::IsNullOrWhiteSpace($_)) {
                try {
                    ($_ | ConvertFrom-Json).instrument
                } catch {
                    $null
                }
            }
        } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Unique -First $MaxMarkets
    if ($derivedTickers.Count -gt 0) {
        $Tickers = ($derivedTickers -join ",")
    }
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$MetricsJson = Join-Path $LogDir "weather-live-runner-$stamp.metrics.json"
$MetricsSnapshot = Join-Path $LogDir "weather-live-runner.snapshot.json"
$ReconcileReport = Join-Path $LogDir "weather-live-runner-$stamp.reconcile.json"

$cargoArgs = @(
    "run",
    "-p", "eventcontracts-live-runner",
    "--manifest-path", "rust/Cargo.toml",
    "--",
    "--duration-secs", "$DurationSeconds",
    "--strategy-spec", $StrategySpec,
    "--sleeve-spec", $SleeveSpec,
    "--external-signals-jsonl", $SignalFile,
    "--external-signal-source", "open-meteo",
    "--external-signals-poll-ms", "250",
    "--external-signal-max-age-secs", "180",
    "--metrics-json", $MetricsJson,
    "--metrics-snapshot-file", $MetricsSnapshot,
    "--reconcile-report", $ReconcileReport,
    "--portfolio-max-gross", "5",
    "--portfolio-group-limit", "weather=5",
    "--portfolio-group-rule", "kalshi:KXHIGH=weather"
)

if ([string]::IsNullOrWhiteSpace($Tickers)) {
    $cargoArgs += @("--pattern", $Pattern, "--max-markets", "$MaxMarkets")
} else {
    $cargoArgs += @("--tickers", $Tickers)
}

if ($Mode -eq "live") {
    $cargoArgs += @(
        "--live-submit",
        "--max-live-orders", "$MaxLiveOrders",
        "--seed-open-market-state-on-start"
    )
    # Both policies seed positions/balance/daily-loss before trading. They
    # differ only in resting-order disposition: reconcile adopts them (and
    # aborts if one is un-adoptable), cancel-orphans cancels ALL resting orders.
    if ($CancelOrphans) {
        $cargoArgs += "--cancel-orphans-on-start"
    } else {
        $cargoArgs += "--reconcile-on-start"
    }
    if ($Yes) {
        $cargoArgs += "--yes"
    }
}

Write-Host "Starting weather Rust live runner."
Write-Host "mode=$Mode"
if ($Mode -eq "live") {
    if ($CancelOrphans) {
        Write-Host "resting_order_policy=cancel-orphans-on-start (cancels ALL resting venue orders)"
    } else {
        Write-Host "resting_order_policy=reconcile-on-start (adopt; aborts if any resting order is un-adoptable)"
    }
}
Write-Host "signal_file=$SignalFile"
Write-Host "strategy=$StrategySpec"
Write-Host "sleeve=$SleeveSpec"
Write-Host "metrics_snapshot=$MetricsSnapshot"
if ($Mode -eq "live" -and -not $Yes) {
    Write-Host "The runner will ask you to type 'yes' before any live order can submit."
}

& cargo @cargoArgs
