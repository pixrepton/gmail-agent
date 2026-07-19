param(
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoName = Split-Path $repoRoot -Leaf

if (-not $OutputPath) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $OutputPath = Join-Path (Split-Path $repoRoot -Parent) "$repoName-clean-export-$stamp.zip"
}

$OutputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)

if (Test-Path -LiteralPath $OutputPath) {
    throw "Refusing to overwrite existing export: $OutputPath"
}

function Test-ExcludedPath {
    param([string]$RelativePath)

    $rel = $RelativePath -replace "\\", "/"
    $segments = $rel -split "/"
    $leaf = Split-Path $rel -Leaf

    $excludedSegments = @(
        ".git",
        ".gitnexus",
        "graphify-out",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".vscode",
        ".playwright-mcp",
        ".serena",
        ".claude",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "vendor",
        "__pycache__",
        "htmlcov",
        "coverage"
    )

    foreach ($segment in $segments) {
        if ($excludedSegments -contains $segment) {
            return $true
        }
    }

    if ($rel -like "runs/*") { return $true }
    if ($rel -like "tools/gmail_audit/runs/*") { return $true }
    if ($rel -like "Daszek/uploads/*") { return $true }

    if ($leaf -like ".env*") { return $true }
    if ($leaf -in @("id_rsa", "id_ed25519", "credentials.json", "token.json")) { return $true }
    if ($leaf -like "*_rsa") { return $true }
    if ($leaf -like "*_ed25519") { return $true }
    if ($leaf -like "*.pem") { return $true }
    if ($leaf -like "*.key") { return $true }
    if ($leaf -like "*.ppk") { return $true }

    if ($leaf -like "*.pyc") { return $true }
    if ($leaf -like "*.pyo") { return $true }
    if ($leaf -like "*.tmp") { return $true }
    if ($leaf -like "*.bak") { return $true }
    if ($leaf -like "*.log") { return $true }
    if ($leaf -like "*.sqlite") { return $true }
    if ($leaf -like "*.sqlite3") { return $true }
    if ($leaf -like "*.db") { return $true }
    if ($leaf -like "*.db-shm") { return $true }
    if ($leaf -like "*.db-wal") { return $true }

    if ($leaf -like "*.zip") { return $true }
    if ($leaf -like "*.tar") { return $true }
    if ($leaf -like "*.tar.gz") { return $true }
    if ($leaf -like "*.tgz") { return $true }
    if ($leaf -like "*.7z") { return $true }
    if ($leaf -like "*.rar") { return $true }

    return $false
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$zip = [System.IO.Compression.ZipFile]::Open($OutputPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    Get-ChildItem -LiteralPath $repoRoot -Recurse -Force -File | ForEach-Object {
        $relative = $_.FullName.Substring($repoRoot.Length).TrimStart("\", "/")
        $relative = $relative -replace "\\", "/"

        if (Test-ExcludedPath $relative) {
            return
        }

        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $relative) | Out-Null
    }
}
finally {
    $zip.Dispose()
}

Write-Output $OutputPath
