#!/usr/bin/env bash
# Installs the Telegram alerts: a timer that checks setups 5/6 every minute (09:00-14:59 IST, Mon-Fri)
# and sends BUY / SELL messages. Usage (from the project folder):  bash deploy/setup_signals.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
PY="$APP_DIR/.venv/bin/python"
ENV="$APP_DIR/.env"
cd "$APP_DIR"

set_env() {   # set_env KEY VALUE: replace or append in .env
    if grep -q "^$1=" "$ENV" 2>/dev/null; then sed -i "s|^$1=.*|$1=$2|" "$ENV"; else echo "$1=$2" >> "$ENV"; fi
}

if ! grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$ENV" 2>/dev/null; then
    echo "==> Telegram bot token (in Telegram: message @BotFather, send /newbot, copy the token)"
    read -rsp "Bot token: " TOKEN; echo
    set_env TELEGRAM_BOT_TOKEN "$TOKEN"
fi
if ! grep -q '^TELEGRAM_CHAT_ID=.\+' "$ENV" 2>/dev/null; then
    echo "==> Now open your bot in Telegram and send it any message (e.g. hi). Press Enter when done."
    read -r
    "$PY" -m market_agent.telegram --find-chat
    read -rp "Chat id to send alerts to (number from the line above): " CHAT
    set_env TELEGRAM_CHAT_ID "$CHAT"
fi
chmod 600 "$ENV"

echo "==> Sending a test message"
"$PY" -m market_agent.telegram --test

echo "==> Installing systemd service + timer"
for unit in marketdatasignals.service marketdatasignals.timer; do
    sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$RUN_USER|g" \
        "$APP_DIR/deploy/$unit" | sudo tee "/etc/systemd/system/$unit" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now marketdatasignals.timer

echo "==> Done. Next runs:"
systemctl list-timers marketdatasignals.timer --no-pager
echo "Log: journalctl -u marketdatasignals -f"
