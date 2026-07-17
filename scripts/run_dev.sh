#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Starting interface_v2 backend on http://127.0.0.1:8000"
(
  cd "$ROOT_DIR"
  python3 -m uvicorn backend.app.main:app --reload --port 8000 --no-proxy-headers
) &
BACKEND_PID=$!

cleanup() {
  kill "$BACKEND_PID" >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

echo "Starting interface_v2 frontend on http://127.0.0.1:5173"
cd "$ROOT_DIR/frontend"
npm run dev -- --host 0.0.0.0
