#!/usr/bin/env bash
# Installs the PCR exporter: a timer that writes Data/<INDEX>/PCR/... every minute in market hours.
# Usage (from the project folder, after deploy/setup_ubuntu.sh):  bash deploy/setup_pcr.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"

echo "==> Installing systemd service + timer"
for unit in marketdatapcr.service marketdatapcr.timer; do
    sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$RUN_USER|g" \
        "$APP_DIR/deploy/$unit" | sudo tee "/etc/systemd/system/$unit" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now marketdatapcr.timer

echo "==> Building PCR files for all data collected so far"
"$APP_DIR/.venv/bin/python" "$APP_DIR/analysis/export_pcr.py" --all

echo "==> Done. Next runs:"
systemctl list-timers marketdatapcr.timer --no-pager
