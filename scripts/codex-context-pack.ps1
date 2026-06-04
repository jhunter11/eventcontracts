param(
    [ValidateSet("all", "weather", "tennis", "btc", "macro", "sports", "rust", "docs")]
    [string]$Surface = "all",
    [string]$Repo = (Resolve-Path ".").Path
)

$ErrorActionPreference = "Stop"
Set-Location -Path $Repo

function Show-Existing {
    param(
        [string]$Title,
        [string[]]$Paths
    )
    Write-Host ""
    Write-Host "## $Title"
    foreach ($path in $Paths) {
        if (Test-Path $path) { Write-Host $path }
    }
}

Write-Host "# Eventcontracts Context Pack: $Surface"
Write-Host "Read repo AGENTS.md first. Use public/read-only or paper paths unless policy explicitly allows more."

if ($Surface -in @("all", "weather")) {
    Show-Existing "Weather" @(
        "python\src\eventcontracts\weather",
        "python\src\eventcontracts\plugins\strategies\weather_temperature_arbitrage.py",
        "configs\strategies\weather-temperature-arbitrage.toml",
        "configs\sleeves\weather-kalshi-paper-a.toml",
        "python\tests\test_weather_kxhigh.py",
        "python\tests\test_weather_distribution.py",
        "docs\weather-kxhigh-validation-and-edge-spec.md"
    )
}

if ($Surface -in @("all", "tennis", "sports")) {
    Show-Existing "Tennis And Sports" @(
        "python\src\eventcontracts\plugins\strategies\sports_tennis_xgboost.py",
        "python\src\eventcontracts\research\tennis_v2.py",
        "python\src\eventcontracts\research\tennis_market_state.py",
        "contracts\parity\sports_tennis_xgboost",
        "python\tests\test_sports_tennis_xgboost_strategy.py",
        "python\tests\test_tennis_v2_research.py",
        "docs\tennis-tradeability-findings-and-plan.md"
    )
}

if ($Surface -in @("all", "btc")) {
    Show-Existing "BTC Research" @(
        "python\src\eventcontracts\research\btc_settlement.py",
        "python\src\eventcontracts\research\btc_lead.py",
        "python\scripts\btc_settlement_bench.py",
        "python\scripts\btc_settlement_gap.py",
        "python\scripts\btc_clead_recorder.py",
        "python\tests\test_btc_settlement.py",
        "python\tests\test_btc_lead.py",
        "docs\kalshi-btc-settlement-arb-validation.md"
    )
}

if ($Surface -in @("all", "macro")) {
    Show-Existing "Macro" @(
        "python\src\eventcontracts\research\cpi_nowcast.py",
        "python\src\eventcontracts\research\fed_watch.py",
        "configs\strategies\macro-cpi-cdf.toml",
        "configs\strategies\macro-fed-gnn.toml",
        "python\tests\test_macro_models.py"
    )
}

if ($Surface -in @("all", "rust")) {
    Show-Existing "Rust Runtime" @(
        "rust\Cargo.toml",
        "rust\crates\runner",
        "rust\crates\risk",
        "rust\crates\gateway",
        "rust\crates\kalshi",
        "docs\runbooks\kalshi-live-runner.md",
        "docs\strategy-runner-contract.md"
    )
}

if ($Surface -in @("all", "docs")) {
    Show-Existing "Agent Workflow Docs" @(
        "docs\agent-known-gotchas.md",
        "docs\agent-definition-of-done.md",
        "docs\agent-failure-playbooks.md",
        "docs\strategy-promotion-checklist.md",
        "docs\codex-workflow-hardening.md"
    )
}
