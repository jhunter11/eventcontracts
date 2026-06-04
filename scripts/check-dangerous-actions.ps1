param(
    [string]$Repo = (Resolve-Path ".").Path,
    [string[]]$Path = @(),
    [switch]$IncludeDocs,
    [switch]$ScanUntracked,
    [switch]$AllowLiveSubmit,
    [switch]$AllowOrderWrites
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

function Should-SkipFile {
    param([string]$File)
    $normalized = $File -replace "\\", "/"
    if ($normalized -eq "scripts/check-dangerous-actions.ps1") { return $true }
    if (-not $IncludeDocs -and $normalized -match "\.md$") { return $true }
    if ($normalized -match "^\.claude/" -or $normalized -match "^\.codex/") { return $true }
    return $false
}

function Test-DangerLine {
    param([string]$Line)
    $patterns = @()
    if (-not $AllowLiveSubmit) {
        $patterns += "--live-submit"
    }
    if (-not $AllowOrderWrites) {
        $patterns += "submit_order"
        $patterns += "place_order"
        $patterns += "create_order"
        $patterns += "cancel_order"
        $patterns += "cancel_all"
        $patterns += "/orders"
        $patterns += "orders/"
    }

    foreach ($pattern in $patterns) {
        if ($Line -match [regex]::Escape($pattern)) {
            return $pattern
        }
    }
    return $null
}

$hits = @()
$explicitPaths = @(Expand-List $Path)

if ($explicitPaths.Count -gt 0) {
    foreach ($file in $explicitPaths) {
        if (Should-SkipFile $file) { continue }
        $full = Join-Path $Repo $file
        if (-not (Test-Path $full)) { continue }
        $lineNo = 0
        foreach ($line in Get-Content $full) {
            $lineNo += 1
            $hit = Test-DangerLine $line
            if ($hit) {
                $hits += [pscustomobject]@{ File = $file; Line = $lineNo; Pattern = $hit; Text = $line.Trim() }
            }
        }
    }
} else {
    $diffLines = @(git diff --unified=0 2>$null) + @(git diff --cached --unified=0 2>$null)
    $currentFile = $null
    foreach ($line in $diffLines) {
        if ($line -match "^\+\+\+ b/(.+)$") {
            $currentFile = $Matches[1]
            continue
        }
        if ($null -eq $currentFile -or (Should-SkipFile $currentFile)) { continue }
        if ($line.StartsWith("+") -and -not $line.StartsWith("+++")) {
            $hit = Test-DangerLine $line
            if ($hit) {
                $hits += [pscustomobject]@{ File = $currentFile; Line = "diff"; Pattern = $hit; Text = $line.Substring(1).Trim() }
            }
        }
    }

    if ($ScanUntracked) {
        foreach ($file in git ls-files --others --exclude-standard 2>$null) {
            if (Should-SkipFile $file) { continue }
            $lineNo = 0
            foreach ($line in Get-Content (Join-Path $Repo $file)) {
                $lineNo += 1
                $hit = Test-DangerLine $line
                if ($hit) {
                    $hits += [pscustomobject]@{ File = $file; Line = $lineNo; Pattern = $hit; Text = $line.Trim() }
                }
            }
        }
    }
}

if ($hits.Count -gt 0) {
    Write-Host "Dangerous action candidates found:"
    $hits | Format-Table -AutoSize
    Write-Host "Use explicit approval and a narrower allow switch only after reviewing these lines."
    exit 2
}

Write-Host "Dangerous action scan clean."
