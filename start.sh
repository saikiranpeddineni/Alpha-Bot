#!/usr/bin/env bash
# AlphaDesk launcher (macOS / Linux)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Virtual env missing -- run ./install.sh first." >&2
  exit 1
fi

BIND=$(grep -E '^\s*ALPHADESK_HOST\s*=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ' || true)
PORT=$(grep -E '^\s*ALPHADESK_PORT\s*=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ' || true)
BIND=${BIND:-127.0.0.1}
PORT=${PORT:-8742}

PROTOCOL="http"
if [ -f key.pem ] && [ -f cert.pem ]; then
  PROTOCOL="https"
fi

echo ""
echo "=== AlphaDesk starting on ${PROTOCOL}://${BIND}:${PORT} ==="
if [ "$BIND" = "0.0.0.0" ]; then
  echo "Local access: ${PROTOCOL}://127.0.0.1:${PORT}"
fi
echo "Press Ctrl+C to stop."
echo ""
exec .venv/bin/python backend/run_server.py
