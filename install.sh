#!/usr/bin/env bash
# One-time setup.
#   ./install.sh           Python install on this machine (Linux / macOS), then ./start.sh
#   ./install.sh --docker  only the config files, for `docker compose up -d --build`
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" != "--docker" ]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Потрібен Python 3.11+ (Ubuntu: sudo apt install python3 python3-venv)"; exit 1
  fi
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || { echo "Потрібен Python 3.11 або новіший"; exit 1; }
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e ".[ai]"
fi

mkdir -p data
[ -f config.toml ] || cp config.example.toml config.toml
if [ ! -f .env ]; then
  cp .env.example .env
  pw=$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))' 2>/dev/null || openssl rand -base64 12 | tr -d '/+=')
  sed -i.bak "s|^BH_WEB_PASSWORD=.*|BH_WEB_PASSWORD=$pw|" .env && rm -f .env.bak
  echo "Пароль веб-панелі: $pw (збережено в .env)"
fi

echo
echo "Готово. Відкрий .env і впиши BH_RPC_URL та BH_TELEGRAM_TOKEN."
if [ "${1:-}" = "--docker" ]; then
  echo "Потім: docker compose up -d --build"
else
  echo "Потім: ./start.sh"
fi
