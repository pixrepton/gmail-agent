# Copy gmail-agent tree for handoff using the shared Python export hardening engine.
param(
    [Parameter(Mandatory = $false)]
    [string] $Source = ".",
    [Parameter(Mandatory = $true)]
    [string] $Destination
)
$ErrorActionPreference = "Stop"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "python is required for clean_export.ps1"
}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$engine = Join-Path $here 'export_hardening.py'
if (-not (Test-Path -LiteralPath $engine)) {
    Write-Error "export_hardening.py not found next to clean_export.ps1"
}
if (-not (Test-Path -LiteralPath $Source)) {
    Write-Error "Source path does not exist: $Source"
}
$srcAbs = (Resolve-Path -LiteralPath $Source).Path
$dstRoot = Split-Path -Parent $Destination
if ($dstRoot -and -not (Test-Path -LiteralPath $dstRoot)) {
    New-Item -ItemType Directory -Force -Path $dstRoot | Out-Null
}
& $python.Source $engine clean $srcAbs $Destination
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
