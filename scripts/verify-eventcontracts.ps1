param(
    [string]$Repo = (Resolve-Path ".").Path,
    [string[]]$PythonTests = @(),
    [string[]]$RuffTargets = @(),
    [string[]]$MypyTargets = @(),
    [switch]$CargoWorkspace,
    [switch]$ChangedOnly,
    [switch]$RunDangerScan,
    [switch]$ListOnly
)

$ErrorActionPreference = "Stop"
Set-Location -Path $Repo

function Expand-List {
    param([string[]]$Items)
    $out = @()
    foreach ($item in $Items) {
        foreach ($part in ($item -split ",")) {
            $trimmed = $part.Trim()
            if ($trimmed) { $out += $trimmed }
        }
    }
    return $out
}

function Invoke-Gate {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    Write-Host "== $Name =="
    & $Command
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    Write-Host "exit=$code"
    if ($code -ne 0) { exit $code }
}

function Get-ChangedFiles {
    $files = @()
    $commands = @(
        @("diff", "--name-only"),
        @("diff", "--cached", "--name-only"),
        @("ls-files", "--others", "--exclude-standard")
    )
    foreach ($gitArgs in $commands) {
        try {
            $files += git @gitArgs 2>$null
        } catch {
            continue
        }
    }
    return @($files | Where-Object { $_ } | Sort-Object -Unique)
}

function Add-IfExists {
    param(
        [string[]]$Items,
        [string]$Path
    )
    if (Test-Path (Join-Path $Repo $Path)) {
        return @($Items + $Path)
    }
    return $Items
}

$PythonTests = @(Expand-List $PythonTests)
$RuffTargets = @(Expand-List $RuffTargets)
$MypyTargets = @(Expand-List $MypyTargets)

if ($ChangedOnly) {
    $changed = @(Get-ChangedFiles)
    Write-Host "Changed files considered: $($changed.Count)"

    foreach ($file in $changed) {
        $normalized = $file -replace "\\", "/"
        if ($normalized -match "^python/tests/.*\.py$") {
            $PythonTests += $file
            $RuffTargets += $file
            $MypyTargets += $file
            continue
        }
        if ($normalized -match "^python/(src|scripts)/.*\.py$") {
            $RuffTargets += $file
            $MypyTargets += $file
            $stem = [System.IO.Path]::GetFileNameWithoutExtension($file)
            $mapped = "python/tests/test_$stem.py"
            $PythonTests = @(Add-IfExists $PythonTests $mapped)
            if ($normalized -match "weather") {
                $PythonTests = @(Add-IfExists $PythonTests "python/tests/test_weather_kxhigh.py")
                $PythonTests = @(Add-IfExists $PythonTests "python/tests/test_weather_distribution.py")
                $PythonTests = @(Add-IfExists $PythonTests "python/tests/test_weather_calibration.py")
            }
            if ($normalized -match "tennis") {
                $PythonTests = @(Add-IfExists $PythonTests "python/tests/test_sports_tennis_xgboost_strategy.py")
                $PythonTests = @(Add-IfExists $PythonTests "python/tests/test_tennis_v2_research.py")
            }
            if ($normalized -match "btc") {
                $PythonTests = @(Add-IfExists $PythonTests "python/tests/test_btc_settlement.py")
                $PythonTests = @(Add-IfExists $PythonTests "python/tests/test_btc_lead.py")
            }
            continue
        }
        if ($normalized -match "^contracts/parity/") {
            $PythonTests = @(Add-IfExists $PythonTests "python/tests/test_strategy_specs.py")
            $PythonTests = @(Add-IfExists $PythonTests "python/tests/test_strategy_runner.py")
        }
        if ($normalized -match "^rust/.*\.rs$" -or $normalized -match "^rust/.*/Cargo\.toml$") {
            $CargoWorkspace = $true
        }
    }

    $PythonTests = @($PythonTests | Sort-Object -Unique)
    $RuffTargets = @($RuffTargets | Sort-Object -Unique)
    $MypyTargets = @($MypyTargets | Sort-Object -Unique)
}

$python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

if ($ListOnly) {
    Write-Host "Planned verification gates:"
    Write-Host "dangerous action scan: $RunDangerScan"
    Write-Host "pytest targets: $($PythonTests -join ', ')"
    Write-Host "ruff targets: $($RuffTargets -join ', ')"
    Write-Host "mypy targets: $($MypyTargets -join ', ')"
    Write-Host "cargo workspace: $CargoWorkspace"
    exit 0
}

if ($RunDangerScan) {
    $scanner = Join-Path $Repo "scripts\check-dangerous-actions.ps1"
    if (Test-Path $scanner) {
        Invoke-Gate "dangerous action scan" { powershell -ExecutionPolicy Bypass -File $scanner -Repo $Repo }
    } else {
        Write-Host "Skipping dangerous action scan; missing $scanner"
    }
}

if ($PythonTests.Count -gt 0) {
    Invoke-Gate "pytest" { & $python -m pytest @PythonTests -q }
}

if ($RuffTargets.Count -gt 0) {
    Invoke-Gate "ruff" { & $python -m ruff check @RuffTargets }
}

if ($MypyTargets.Count -gt 0) {
    Invoke-Gate "mypy" { & $python -m mypy @MypyTargets }
}

if ($CargoWorkspace) {
    Invoke-Gate "cargo workspace tests" { cargo test --manifest-path rust\Cargo.toml --workspace }
}

if ($PythonTests.Count -eq 0 -and $RuffTargets.Count -eq 0 -and $MypyTargets.Count -eq 0 -and -not $CargoWorkspace -and -not $RunDangerScan) {
    Write-Host "No gates requested. Pass -PythonTests, -RuffTargets, -MypyTargets, -CargoWorkspace, or -ChangedOnly."
}
