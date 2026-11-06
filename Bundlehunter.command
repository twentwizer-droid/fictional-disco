#!/bin/bash
# bundlehunter for macOS (Intel and Apple Silicon): double-click this file in Finder.
#
#   first run:  installs Python 3.12 (no admin password needed), the bot and the overlay,
#               asks for your Solana RPC address and (optionally) a Telegram bot token
#   every run:  starts the bot on PAPER money, opens the dashboard and the overlay window
#
# Close the overlay or this Terminal window to stop everything.
# If macOS says the file is from an unidentified developer: right-click it -> Open -> Open.
# If double-click does nothing: Terminal -> chmod +x Bundlehunter.command
set -u
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
PORT=8080
LOG=data/serve.log

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() {
  osascript -e "display alert \"bundlehunter\" message \"$1\" as critical" >/dev/null 2>&1
  echo "ПОМИЛКА: $1"; read -r -p "Натисни Enter, щоб закрити…" _; exit 1
}
ask() {  # ask "question" "default" -> prints the answer, empty if skipped
  osascript -e "try
    text returned of (display dialog \"$1\" default answer \"$2\" buttons {\"Пропустити\", \"OK\"} default button \"OK\" with title \"bundlehunter\")
  on error
    return \"\"
  end try" 2>/dev/null
}
set_env() {  # set_env KEY value  (in .env, macOS sed)
  local v
  v=$(printf '%s' "$2" | sed 's/[&|\\]/\\&/g')  # & | \ are special in a sed replacement
  if grep -q "^$1=" .env; then
    sed -i '' "s|^$1=.*|$1=$v|" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

# -- 1. Python 3.11+ ---------------------------------------------------------------------------
find_python() {
  for p in python3.13 python3.12 python3.11 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$p"; return 0
    fi
  done
  return 1
}

if [ ! -x .venv/bin/bh ]; then
  say "Перший запуск: встановлюю все потрібне (3–5 хвилин)…"
  if PY=$(find_python); then
    echo "Python: $PY"
    "$PY" -m venv .venv || die "не вдалося створити .venv"
    PIP=".venv/bin/pip install --quiet"
    .venv/bin/pip install --quiet --upgrade pip
  else
    # macOS ships Python 3.9: take a private Python 3.12 via uv (in your home folder, no sudo)
    if ! command -v uv >/dev/null 2>&1; then
      echo "Завантажую uv (менеджер Python від Astral)…"
      curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null || die "не вдалося завантажити uv: перевір інтернет"
    fi
    uv venv --quiet --python 3.12 .venv || die "не вдалося встановити Python 3.12"
    PIP="uv pip install --quiet --python .venv/bin/python"
  fi
  $PIP -e ".[ai]" || die "не вдалося встановити бота (дивись повідомлення вище)"
  $PIP "PySide6-Essentials>=6.6" || echo "Оверлей не встановився (стара macOS?). Бот і веб-панель працюватимуть без нього."
fi

# -- 2. config and secrets (stay on this Mac) ---------------------------------------------------
mkdir -p data
[ -f config.toml ] || cp config.example.toml config.toml
if [ ! -f .env ]; then
  cp .env.example .env
  set_env BH_WEB_PASSWORD "$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(12))')"
fi
if grep -q '^BH_RPC_URL=.*YOUR_KEY' .env || ! grep -q '^BH_RPC_URL=.' .env; then
  rpc=$(ask "Встав адресу Solana RPC (Helius: https://mainnet.helius-rpc.com/?api-key=...) або лише API-ключ Helius.\n\nЦе НЕ приватний ключ гаманця. Зберігається тільки в .env на цьому Mac." "")
  if [ -n "$rpc" ]; then
    case "$rpc" in http*) ;; *) rpc="https://mainnet.helius-rpc.com/?api-key=$rpc" ;; esac
    set_env BH_RPC_URL "$rpc"
  else
    echo "Без RPC бот не бачитиме блокчейн. Впиши BH_RPC_URL у файл .env і запусти ще раз."
  fi
fi
if ! grep -q '^BH_TELEGRAM_TOKEN=.' .env && [ ! -f data/.telegram_asked ]; then
  touch data/.telegram_asked
  tg=$(ask "Токен Telegram-бота від @BotFather (необов'язково — можна пропустити)." "")
  [ -n "$tg" ] && set_env BH_TELEGRAM_TOKEN "$tg"
fi

# -- 3. start ----------------------------------------------------------------------------------
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  say "Бот уже працює (порт $PORT зайнятий) — відкриваю панель і оверлей."
  SERVE_PID=""
else
  say "Запускаю бота (папір, без реальних грошей). Лог: $LOG"
  .venv/bin/bh serve >>"$LOG" 2>&1 &
  SERVE_PID=$!
  stop() { if [ -n "$SERVE_PID" ]; then kill "$SERVE_PID" 2>/dev/null; SERVE_PID=""; echo "Бота зупинено."; fi; }
  trap 'stop' EXIT
  trap 'stop; exit 0' INT TERM HUP
  for _ in $(seq 1 60); do
    curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break
    kill -0 "$SERVE_PID" 2>/dev/null || { tail -n 30 "$LOG"; die "бот зупинився одразу: дивись data/serve.log"; }
    sleep 1
  done
fi

pw=$(grep '^BH_WEB_PASSWORD=' .env | cut -d= -f2-)
open "http://127.0.0.1:$PORT/"
[ -n "$pw" ] && echo "Веб-панель: будь-яке ім'я, пароль: $pw"

if .venv/bin/python -c 'import PySide6.QtWidgets' 2>/dev/null; then
  say "Оверлей відкрито. Закрий його (або це вікно), щоб зупинити бота."
  .venv/bin/bh overlay
else
  say "Бот працює. Закрий це вікно або натисни Ctrl+C, щоб зупинити."
  [ -n "$SERVE_PID" ] && wait "$SERVE_PID"
fi
