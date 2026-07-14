# AlphaDesk dev mode: backend with reload + Vite HMR frontend (http://localhost:5173)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Write-Host "Run .\install.ps1 first." -ForegroundColor Red; exit 1 }

Write-Host "Backend: http://127.0.0.1:8742   Frontend (HMR): http://localhost:5173" -ForegroundColor Cyan
$backend = Start-Process -PassThru -NoNewWindow $venvPy -ArgumentList @(
    "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8742", "--reload"
)
try {
    Set-Location frontend
    npm run dev
} finally {
    if ($backend -and !$backend.HasExited) { Stop-Process -Id $backend.Id -Force }
}
