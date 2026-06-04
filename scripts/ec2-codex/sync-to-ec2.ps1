<#
  Sync the repo to the EC2 box from PowerShell (no bash/Git Bash needed).

  Uses native tar + scp + ssh. Ships your real .env + ecmodel.txt (Kalshi private
  key) + demokey.txt ON PURPOSE; excludes SSH login keys, .git, venv, heavy data.
  Tars to a temp FILE then scp's it (PowerShell 5.1 corrupts binary in native
  pipes, so no streaming) and extracts remotely.

  ASCII-only on purpose: PowerShell 5.1 reads .ps1 in the ANSI codepage, so any
  non-ASCII char (em-dash, smart quote) corrupts string literals. Keep it ASCII.

  Usage:
    ./scripts/ec2-codex/sync-to-ec2.ps1 -HostSpec ubuntu@44.211.233.167 -Key C:\Users\jachu\Downloads\mainkey.pem
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$HostSpec,
  [Parameter(Mandatory = $true)][string]$Key,
  [string]$RemoteDir = "eventcontracts"
)
$ErrorActionPreference = "Stop"

# Repo root = two levels up from this script.
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoRoot
Write-Host "repo_root = $RepoRoot"

if (-not (Test-Path $Key)) { throw "SSH key not found: $Key" }

# Windows OpenSSH refuses group/world-readable keys; lock ACLs to current user.
icacls $Key /inheritance:r | Out-Null
icacls $Key /grant:r "$($env:USERNAME):(R)" | Out-Null

# --- exclusion boundary ---
# Kalshi material (.env, ecmodel.txt, demokey.txt) IS shipped on purpose.
# Excluded: SSH login keys, git history, build/scratch, heavy data.
$excludes = @(
  '*.pem','id_rsa*','id_ed25519*','.ssh',
  '.git','.venv','rust/target','__pycache__',
  '.mypy_cache','.pytest_cache','.ruff_cache','.tmp-target','.tmp-runpod',
  '*.onnx','*.parquet','data','artifacts'
)
$exArgs = $excludes | ForEach-Object { "--exclude=$_" }

$tarball = Join-Path $env:TEMP ("ec-sync-{0}.tar.gz" -f (Get-Date -Format yyyyMMdd-HHmmss))

Write-Host "==> Creating payload tarball (native tar)"
& tar @exArgs -czf $tarball -C $RepoRoot .
if ($LASTEXITCODE -ne 0) { throw "tar failed ($LASTEXITCODE)" }

# --- verify the boundary BEFORE anything leaves the machine ---
$list = & tar -tzf $tarball
$leak = $list | Select-String -Pattern '(^|/)\.pem$','(^|/)id_rsa','(^|/)id_ed25519','(^|/)\.ssh/','(^|/)\.git/'
if ($leak) {
  Remove-Item $tarball -Force
  throw ("SSH/git secret survived excludes - aborting: " + ($leak -join '; '))
}
$ship = $list | Select-String -Pattern '(^|/)\.env$','(^|/)ecmodel\.txt$','(^|/)demokey\.txt$'
Write-Host "    Kalshi material in payload:"
$ship | ForEach-Object { Write-Host "      ship: $_" }
if (-not $ship) { Write-Host "      WARN: expected .env/ecmodel.txt/demokey.txt not found!" -ForegroundColor Yellow }
$sizeMB = [math]::Round((Get-Item $tarball).Length / 1MB, 1)
Write-Host ("    payload = {0} MB, {1} entries" -f $sizeMB, ($list | Measure-Object).Count)

$sshKeyOpts = @('-i', $Key, '-o', 'StrictHostKeyChecking=accept-new')

Write-Host "==> Ensuring remote ~/$RemoteDir and copying tarball"
& ssh @sshKeyOpts $HostSpec "mkdir -p ~/$RemoteDir"
if ($LASTEXITCODE -ne 0) { Remove-Item $tarball -Force; throw "remote mkdir failed" }
& scp @sshKeyOpts $tarball ("{0}:~/{1}/_payload.tar.gz" -f $HostSpec, $RemoteDir)
if ($LASTEXITCODE -ne 0) { Remove-Item $tarball -Force; throw "scp failed" }

# Ship the remote setup script separately so it exists BEFORE extraction, and so
# PowerShell never has to embed/escape bash (the brittle part). It just runs it.
$remoteSetup = Join-Path $PSScriptRoot "remote-setup.sh"
& scp @sshKeyOpts $remoteSetup ("{0}:~/{1}/_remote-setup.sh" -f $HostSpec, $RemoteDir)
if ($LASTEXITCODE -ne 0) { Remove-Item $tarball -Force; throw "scp of remote-setup failed" }

Write-Host "==> Extracting, locking key perms, verifying key path"
# tr -d '\r' guards against CRLF if the file ever gets Windows line endings.
& ssh @sshKeyOpts $HostSpec "tr -d '\r' < ~/$RemoteDir/_remote-setup.sh | bash -s -- $RemoteDir; rm -f ~/$RemoteDir/_remote-setup.sh"

Remove-Item $tarball -Force
Write-Host ""
Write-Host "==> Sync complete. Next, on the box:" -ForegroundColor Green
Write-Host "    ssh -i `"$Key`" $HostSpec"
Write-Host "    cd ~/$RemoteDir; bash scripts/ec2-codex/bootstrap-ec2.sh"
Write-Host "    # then point your running Codex at ~/$RemoteDir (it reads AGENTS.md)"
