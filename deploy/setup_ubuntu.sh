#!/usr/bin/env bash
# One-time setup on Ubuntu 24.04: virtualenv + systemd timer (Mon-Fri 09:05 IST).
# Usage (from the project folder):  bash deploy/setup_ubuntu.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"

echo "==> Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

echo "==> Creating virtualenv in $APP_DIR/.venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "!! Created $APP_DIR/.env - fill in your Angel One credentials before the first run."
fi

echo "==> Installing systemd service + timer"
for unit in marketdataagent.service marketdataagent.timer; do
    sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$RUN_USER|g" \
        "$APP_DIR/deploy/$unit" | sudo tee "/etc/systemd/system/$unit" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now marketdataagent.timer

echo "==> Done. Next run:"
systemctl list-timers marketdataagent.timer --no-pager
