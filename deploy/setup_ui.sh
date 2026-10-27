#!/usr/bin/env bash
# Installs the analysis dashboard as an always-on service on the server.
# Usage (from the project folder, after deploy/setup_ubuntu.sh):
#   bash deploy/setup_ui.sh            # private: 127.0.0.1:8501, open it through an SSH tunnel
#   bash deploy/setup_ui.sh --public   # internet: https://<server-ip>:8501 with login (username + 2 passwords)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
PUBLIC=0
[[ "${1:-}" == "--public" ]] && PUBLIC=1

echo "==> Installing dashboard packages"
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements-ui.txt"

if [[ $PUBLIC == 1 ]]; then
    if ! grep -q '^DASHBOARD_PASS2_HASH=' "$APP_DIR/.env" 2>/dev/null; then
        echo "==> Set the dashboard login (username + two passwords)"
        "$APP_DIR/.venv/bin/python" "$APP_DIR/analysis/set_dashboard_password.py"
    fi
    CERT_DIR="$APP_DIR/certs"
    SERVER_IP="$(hostname -I | awk '{print $1}')"
    if [[ ! -f "$CERT_DIR/dashboard.crt" ]]; then
        echo "==> Creating a self-signed HTTPS certificate for $SERVER_IP"
        mkdir -p "$CERT_DIR"
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$SERVER_IP" \
            -addext "subjectAltName=IP:$SERVER_IP" \
            -keyout "$CERT_DIR/dashboard.key" -out "$CERT_DIR/dashboard.crt" 2>/dev/null
        chmod 600 "$CERT_DIR/dashboard.key"
    fi
    ADDRESS=0.0.0.0
    TLS_ARGS="--server.sslCertFile $CERT_DIR/dashboard.crt --server.sslKeyFile $CERT_DIR/dashboard.key"
    if command -v ufw > /dev/null && sudo ufw status | grep -q "Status: active"; then
        echo "==> Opening port 8501 in ufw"
        sudo ufw allow 8501/tcp
    fi
else
    ADDRESS=127.0.0.1
    TLS_ARGS=""
fi

echo "==> Installing systemd service"
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$RUN_USER|g" -e "s|__PUBLIC__|$PUBLIC|g" \
    -e "s|__ADDRESS__|$ADDRESS|g" -e "s|__TLS_ARGS__|$TLS_ARGS|g" \
    "$APP_DIR/deploy/marketdataui.service" | sudo tee /etc/systemd/system/marketdataui.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now marketdataui.service
sudo systemctl restart marketdataui.service

if [[ $PUBLIC == 1 ]]; then
    echo "==> Done. Open https://$SERVER_IP:8501"
    echo "    The certificate is self-signed: the browser warns once; choose Advanced -> Proceed."
else
    echo "==> Done. From your PC run:"
    echo "    ssh -L 8501:localhost:8501 $RUN_USER@<server-ip>"
    echo "    then open http://localhost:8501"
fi
