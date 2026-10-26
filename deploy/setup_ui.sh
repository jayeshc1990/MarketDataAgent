#!/usr/bin/env bash
# Installs the analysis dashboard as an always-on service on the server (localhost:8501).
# Usage (from the project folder, after deploy/setup_ubuntu.sh):  bash deploy/setup_ui.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"

echo "==> Installing dashboard packages"
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements-ui.txt"

echo "==> Installing systemd service"
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$RUN_USER|g" \
    "$APP_DIR/deploy/marketdataui.service" | sudo tee /etc/systemd/system/marketdataui.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now marketdataui.service
sudo systemctl restart marketdataui.service

echo "==> Done. From your PC run:"
echo "    ssh -L 8501:localhost:8501 $RUN_USER@<server-ip>"
echo "    then open http://localhost:8501"
