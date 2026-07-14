# AlphaDesk installer (Windows PowerShell)
# Creates the Python venv, installs backend + frontend deps, builds the UI.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "=== AlphaDesk install ===" -ForegroundColor Cyan

function Find-Python {
    $candidates = @(
        @{ cmd = "py";      args = @("-3.12") },
        @{ cmd = "py";      args = @("-3.11") },
        @{ cmd = "py";      args = @("-3.10") },
        @{ cmd = "python3"; args = @() },
        @{ cmd = "python";  args = @() }
    )
    foreach ($c in $candidates) {
        try {
            $out = & $c.cmd @($c.args + "--version") 2>$null
            if ($out -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                if ($major -eq 3 -and $minor -ge 10) {
                    return ,($c.cmd, $c.args, "$($Matches[1]).$($Matches[2])")
                }
            }
        } catch { }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "ERROR: Python 3.10+ not found. Install it from https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}
$pyCmd = $py[0]; $pyArgs = $py[1]; $pyVer = $py[2]
Write-Host "[1/4] Python $pyVer found ($pyCmd $pyArgs)"

if (-not (Test-Path ".venv")) {
    & $pyCmd @($pyArgs + @("-m", "venv", ".venv"))
}
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip --quiet
Write-Host "[2/4] Installing backend dependencies (this can take a few minutes)..."
& $venvPy -m pip install -r requirements-dev.txt --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed" -ForegroundColor Red; exit 1 }

$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    Write-Host "ERROR: Node.js 18+ not found. Install it from https://nodejs.org/" -ForegroundColor Red
    exit 1
}
Write-Host "[3/4] Installing frontend dependencies (node $(node --version))..."
Push-Location frontend
npm install --no-fund --no-audit --loglevel=error
if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Host "npm install failed" -ForegroundColor Red; exit 1 }
Write-Host "[4/4] Building the UI..."
npm run build --silent
if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Host "frontend build failed" -ForegroundColor Red; exit 1 }
Pop-Location

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example (demo mode -- no keys needed)"
}

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host "  Start the platform:  .\start.ps1   (or start.cmd)"
Write-Host "  Then open:           http://127.0.0.1:8000"
Write-Host ""
