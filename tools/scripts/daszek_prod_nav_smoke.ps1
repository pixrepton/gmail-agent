# Smoke: production Daszek shell (v1.3.0 PRO nav + app.js safety checks).
$ErrorActionPreference = "Stop"
$base = "https://topinstal.com.pl"
$html = (Invoke-WebRequest -Uri "$base/daszek/" -UseBasicParsing -TimeoutSec 60).Content

if ($html -notmatch 'id="refresh-btn"') {
    Write-Error "PROD_FAIL: missing #refresh-btn. Deploy Daszek index.php."
    exit 1
}
if ($html -notmatch 'id="view-tabs"') {
    Write-Error "PROD_FAIL: missing #view-tabs. Deploy Daszek 1.3.0 index.php to Hostido."
    exit 1
}
if ($html -notmatch 'class="view-tab' -and $html -notmatch 'data-view="desk"') {
    Write-Error "PROD_FAIL: missing PRO top tabs (.view-tab). Re-upload index.php from repo."
    exit 1
}
if ($html -match 'nav class="main-nav"') {
    Write-Error "PROD_FAIL: legacy sidebar main-nav still present."
    exit 1
}

$version = "1.3.0"
$js = $null
foreach ($url in @(
        "$base/wp-content/plugins/Daszek/public/app.js?v=$version",
        "$base/wp-content/plugins/daszek/public/app.js?v=$version",
        "$base/wp-content/plugins/Daszek/public/app.js"
    )) {
    try {
        $js = (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 60).Content
        if ($js.Length -gt 5000) { break }
    }
    catch { }
}
if (-not $js) {
    Write-Error "PROD_FAIL: could not fetch app.js"
    exit 1
}

@('installDaszekNavHandlers', '\.view-tab', 'renderOperatorHero', 'ds-tech-tier-1', 'canSendFeedback') | ForEach-Object {
    if ($js -notmatch $_) {
        Write-Error "PROD_FAIL: app.js missing pattern $_"
        exit 1
    }
}
if ($js.Contains("getElementById('refresh-btn').addEventListener")) {
    Write-Error "PROD_FAIL: unsafe refresh-btn binding"
    exit 1
}
if ($js -match "noteFeedbackBlock\s*=\s*''") {
    Write-Error "PROD_FAIL: feedback still blanked on operational_feed"
    exit 1
}

Write-Host "DASZEK_PROD_NAV_SMOKE_OK version=$version js_bytes=$($js.Length)"
