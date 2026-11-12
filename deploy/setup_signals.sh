#!/usr/bin/env bash
# Installs the Telegram alerts: a timer that checks setups 5/6 every minute (09:00-14:59 IST, Mon-Fri)
# and sends BUY / SELL messages. Usage (from the project folder):  bash deploy/setup_signals.sh
# The bot token and chat id are checked with Telegram before they are saved; a wrong one is asked again.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
PY="$APP_DIR/.venv/bin/python"
ENV="$APP_DIR/.env"
cd "$APP_DIR"
touch "$ENV"
chmod 600 "$ENV"

env_value() { grep "^$1=" "$ENV" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
set_env() {   # set_env KEY VALUE: replace or append in .env
    local tmp; tmp="$(mktemp)"
    grep -v "^$1=" "$ENV" > "$tmp" || true
    echo "$1=$2" >> "$tmp"
    cat "$tmp" > "$ENV"; rm -f "$tmp"
}
# the variables passed here win over .env (see market_agent/config.py load_env)
tg() { TELEGRAM_BOT_TOKEN="$TOKEN" TELEGRAM_CHAT_ID="${CHAT:-}" "$PY" -m market_agent.telegram "$@"; }

# ---- bot token: keep the saved one if Telegram accepts it, else ask until it does
TOKEN="$(env_value TELEGRAM_BOT_TOKEN)"
while true; do
    if [[ -n "$TOKEN" ]] && BOT="$(tg --whoami 2>&1)"; then
        echo "==> Bot token OK: $BOT"
        break
    fi
    [[ -n "$TOKEN" ]] && echo "!! Telegram did not accept that token (${#TOKEN} characters; a real one looks like 1234567890:AAH..., about 46)."
    echo "==> Bot token (in Telegram: @BotFather -> /newbot, or /mybots -> your bot -> API Token)."
    echo "    Paste it with a right-click; nothing is shown while you type."
    read -rsp "Bot token: " TOKEN; echo
    TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"     # drop stray spaces / CR from the paste
done
set_env TELEGRAM_BOT_TOKEN "$TOKEN"

# ---- chat id: keep the saved one if a test message gets through, else find it from a message to the bot
CHAT="$(env_value TELEGRAM_CHAT_ID)"
if [[ -z "$CHAT" ]] || ! tg --test > /dev/null 2>&1; then
    CHAT=""
    while [[ -z "$CHAT" ]]; do
        echo "==> In Telegram open $BOT, press Start and send it any message (e.g. hi). Press Enter when done."
        read -r
        mapfile -t IDS < <(tg --chat-ids | tr -d '\r')
        if (( ${#IDS[@]} == 0 )); then
            echo "!! No message found yet. Make sure you messaged $BOT (not BotFather)."
        elif (( ${#IDS[@]} == 1 )); then
            CHAT="${IDS[0]}"
        else
            tg --find-chat
            read -rp "Several chats found. Chat id to send alerts to: " CHAT
        fi
    done
    echo "==> Sending a test message"
    tg --test
fi
set_env TELEGRAM_CHAT_ID "$CHAT"
echo "==> Telegram OK (chat $CHAT). Check your phone for the test message."

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
