param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [string]$Repo = (Resolve-Path ".").Path,
    [string]$Directory = "live-test\experiments",
    [switch]$NoWrite
)

$ErrorActionPreference = "Stop"
Set-Location -Path $Repo

$slug = ($Name.ToLowerInvariant() -replace "[^a-z0-9]+", "-" -replace "^-|-$", "")
if (-not $slug) { throw "Name must contain at least one letter or digit." }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dir = Join-Path $Repo $Directory
$markdown = Join-Path $dir "$stamp-$slug.md"
$jsonl = Join-Path $dir "$stamp-$slug.jsonl"

$body = @"
# Experiment: $Name

## Hypothesis

State the expected profit mechanism. Avoid calling a model-vs-market gap an edge
until executable pricing, fees, spread, liquidity, freshness, markout, and
settlement are checked.

## Contract Target

- Series/event/market:
- Outcome definition:
- Settlement source:
- Tradable timestamp:
- Executable side and price:

## Data

- Sources:
- Source timestamps:
- Received timestamps:
- Stale-source gates:
- Leakage controls:

## Model And Scoring

- Baseline:
- Candidate model:
- OOS split:
- Brier/log loss:
- Calibration/ECE:

## Execution Adjustment

- Touch price:
- Fees:
- Spread/slippage:
- Position caps/capacity:
- Liquidity notes:

## Decision

Decision: kill / continue research / start tick logging / paper only / promote later

Evidence:

- 
"@

$seed = @"
{"type":"experiment_init","name":"$Name","slug":"$slug","created_at":"$(Get-Date -Format o)","decision":"unreviewed"}
"@

if ($NoWrite) {
    Write-Host "Would create:"
    Write-Host $markdown
    Write-Host $jsonl
    exit 0
}

New-Item -ItemType Directory -Force $dir | Out-Null
Set-Content -Path $markdown -Value $body -Encoding utf8
Set-Content -Path $jsonl -Value $seed -Encoding utf8
Write-Host "Created $markdown"
Write-Host "Created $jsonl"
