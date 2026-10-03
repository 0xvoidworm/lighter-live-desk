#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
test -f config.json || cp config.example.json config.json
uv run --frozen --extra dev lldesk engine &
engine_pid=$!
uv run --frozen --extra dev lldesk api &
api_pid=$!
trap 'kill "$engine_pid" "$api_pid" 2>/dev/null || true' EXIT INT TERM
cd frontend
npm run dev -- --host 127.0.0.1
