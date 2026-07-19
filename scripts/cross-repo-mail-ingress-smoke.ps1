# Optional live mail-ingress template (D1). Canonical contract proof: pytest tools/gmail_audit/tests/test_mail_ingress_contract.py
$ErrorActionPreference = 'Stop'
if (-not $env:MAIL_INGRESS_URL) {
    Write-Error 'Set MAIL_INGRESS_URL (e.g. https://host/wp-json/topinstal/v1/mail-ingress/cieplo-app)'
}
if (-not $env:MAIL_INGRESS_AGENT_KEY) {
    Write-Error 'Set MAIL_INGRESS_AGENT_KEY (do not commit this value)'
}

$body = (@{ smoke = 'd1'; ts = (Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json -Compress)
Write-Host '=== D1 smoke template (run manually, capture redacted transcript) ===' -ForegroundColor Cyan
Write-Host 'Example (adjust URI/body to your contract):'
Write-Host ('$headers = @{ "Content-Type"="application/json"; "X-Top-Instal-Agent-Key" = $env:MAIL_INGRESS_AGENT_KEY }')
Write-Host ('Invoke-WebRequest -Uri $env:MAIL_INGRESS_URL -Method Post -Headers $headers -Body ''' + $body + ''' -SkipHttpErrorCheck')
Write-Host 'See docs/archive/runbooks/CROSS_REPO_LIVE_SMOKE_D1.md for scenario matrix.'
