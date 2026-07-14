# AlphaDesk launcher (Windows PowerShell)
# One process serves the API, WebSocket stream and the built UI.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Virtual env missing -- run .\install.ps1 first." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path "frontend\dist\index.html")) {
    Write-Host "UI not built -- run .\install.ps1 first." -ForegroundColor Yellow
}

$bind = "127.0.0.1"; $port = "8742"
if (Test-Path ".env") {
    foreach ($line in Get-Content ".env") {
        if ($line -match "^\s*ALPHADESK_HOST\s*=\s*(.+)$") { $bind = $Matches[1].Trim() }
        if ($line -match "^\s*ALPHADESK_PORT\s*=\s*(.+)$") { $port = $Matches[1].Trim() }
    }
}

$protocol = "http"
if ((Test-Path "key.pem") -and (Test-Path "cert.pem")) {
    $protocol = "https"
}

Write-Host ""
Write-Host "=== AlphaDesk starting on ${protocol}://${bind}:${port} ===" -ForegroundColor Cyan
Write-Host "Press Ctrl+C to stop."
Write-Host ""

$openHost = $bind
if ($bind -eq "0.0.0.0") { $openHost = "127.0.0.1" }
Start-Process "${protocol}://${openHost}:${port}"
& $venvPy backend\run_server.py
