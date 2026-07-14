#!/usr/bin/env bash
# AlphaDesk installer (macOS / Linux)
set -euo pipefail
cd "$(dirname "$0")"

echo ""
echo "=== AlphaDesk install ==="

PY=""
for candidate in python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    ver=$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "0.0")
    major=${ver%%.*}; minor=${ver##*.}
    if [ "$major" = "3" ] && [ "$minor" -ge 10 ]; then PY="$candidate"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "ERROR: Python 3.10+ not found." >&2
  exit 1
fi
echo "[1/4] Python $($PY --version 2>&1 | cut -d' ' -f2) found ($PY)"

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet
echo "[2/4] Installing backend dependencies (this can take a few minutes)..."
.venv/bin/python -m pip install -r requirements-dev.txt --quiet

if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: Node.js 18+ not found. Install it from https://nodejs.org/" >&2
  exit 1
fi
echo "[3/4] Installing frontend dependencies (node $(node --version))..."
(cd frontend && npm install --no-fund --no-audit --loglevel=error)
echo "[4/4] Building the UI..."
(cd frontend && npm run build --silent)

[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example (demo mode -- no keys needed)"; }

echo ""
echo "Install complete."
echo "  Start the platform:  ./start.sh"
echo "  Then open:           http://127.0.0.1:8000"
echo ""
