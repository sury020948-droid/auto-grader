#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
# Render (and most PaaS) inject $PORT and require binding 0.0.0.0.
exec python3 -m uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
