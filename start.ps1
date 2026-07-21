# AlphaDesk launcher (Windows PowerShell)
# Starts both the backend server and the frontend dev server concurrently.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Virtual env missing -- run .\install.ps1 first." -ForegroundColor Red
    exit 1
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

Write-Host "Starting frontend dev server..." -ForegroundColor Cyan
$feProcess = Start-Process cmd.exe -ArgumentList "/c npm run dev" -WorkingDirectory (Join-Path $PSScriptRoot "frontend") -PassThru -NoNewWindow

Write-Host "=== AlphaDesk starting on ${protocol}://${bind}:${port} ===" -ForegroundColor Cyan
Write-Host "Opening UI dashboard on http://127.0.0.1:5173" -ForegroundColor Cyan
Write-Host "Press Ctrl+C to stop both backend & frontend."
Write-Host ""

try {
    # Give the dev server a brief moment to initialize, then open the browser
    Start-Sleep -Seconds 2
    Start-Process "http://127.0.0.1:5173"
    
    # Run the backend in the foreground
    & $venvPy backend\run_server.py
}
finally {
    if ($feProcess -and -not $feProcess.HasExited) {
        Write-Host "Stopping frontend dev server and cleanup..." -ForegroundColor Yellow
        taskkill /f /t /pid $feProcess.Id 2>$null
    }
}
