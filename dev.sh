#!/usr/bin/env bash
# AlphaDesk dev mode: backend with reload + Vite HMR frontend (http://localhost:5173)
set -euo pipefail
cd "$(dirname "$0")"

[ -x .venv/bin/python ] || { echo "Run ./install.sh first." >&2; exit 1; }

echo "Backend: http://127.0.0.1:8742   Frontend (HMR): http://localhost:5173"
.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8742 --reload &
BACKEND_PID=$!
trap 'kill $BACKEND_PID 2>/dev/null || true' EXIT
(cd frontend && npm run dev)
