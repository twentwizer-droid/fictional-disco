#!/usr/bin/env bash
# Start everything: paper trading + Telegram bot + web panel (http://localhost:8080). Ctrl+C stops it.
cd "$(dirname "$0")"
exec .venv/bin/bh serve "$@"
