# Fail-fast verify using the shared Python export hardening engine.
param(
    [Parameter(Mandatory = $true)]
    [string] $Destination
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Destination -PathType Container)) {
    Write-Error "verify_export_clean: not a directory: $Destination"
}
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "python is required for verify_export_clean.ps1"
}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$engine = Join-Path $here 'export_hardening.py'
if (-not (Test-Path -LiteralPath $engine)) {
    Write-Error "export_hardening.py not found next to verify_export_clean.ps1"
}
& $python.Source $engine verify $Destination
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
