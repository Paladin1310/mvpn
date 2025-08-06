#!/usr/bin/env bash
# install.sh — deploy /opt/ovpn_service.py + OpenVPN + MySQL on Ubuntu 24.04

set -euo pipefail

# ------------------------------------------------------------------
# 1. Checks and variables
# ------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  echo "Please run as root" >&2
  exit 1
fi

OVPN_SERVICE_FILE="/opt/ovpn_service.py"
if [[ ! -f "$OVPN_SERVICE_FILE" ]]; then
  echo "File $OVPN_SERVICE_FILE not found. Copy it before running." >&2
  exit 1
fi

PUBLIC_IP="${1:-}"
if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP=$(curl -s https://api.ipify.org || true)
  [[ -z "$PUBLIC_IP" ]] && {
    echo "Usage: ./install.sh <PUBLIC_IP>" >&2
    exit 1
  }
fi

SERVER_NETWORK="10.100.10.0 255.255.255.0"
# Default OpenVPN listening port
SERVER_PORT="21"

API_TOKEN=$(openssl rand -hex 32)
MYSQL_PASSWORD=$(openssl rand -hex 16)
MYSQL_USER="ovpn_user"
MYSQL_DB="ovpn_panel"
API_PORT="8080"
WORKERS=$(( $(nproc) * 2 ))
VENV_DIR="/opt/ovpn_service_venv"
EASYRSA_DIR="/etc/openvpn/easy-rsa"
TLS_CRYPT_V2_SERVER_KEY="/etc/openvpn/server/tc_v2_server.key"
CLIENT_KEYS_DIR="/etc/openvpn/clients"
STATUS_LOG="/var/log/openvpn-status.log"

# ------------------------------------------------------------------
# 2. System packages
# ------------------------------------------------------------------
echo "==> Installing system packages..."
apt update -y
apt install -y --no-install-recommends openvpn easy-rsa iproute2 python3-venv python3-pip mariadb-server curl

# ------------------------------------------------------------------
# 3. OpenVPN server
# ------------------------------------------------------------------
echo "==> Setting up OpenVPN server..."
mkdir -p /etc/openvpn/server
mkdir -p "$CLIENT_KEYS_DIR"
make-cadir "$EASYRSA_DIR"
cd "$EASYRSA_DIR"
EASYRSA_BATCH=1 ./easyrsa init-pki
EASYRSA_BATCH=1 ./easyrsa build-ca nopass
EASYRSA_BATCH=1 ./easyrsa gen-dh
EASYRSA_BATCH=1 ./easyrsa build-server-full server nopass
cp pki/ca.crt pki/dh.pem pki/private/server.key pki/issued/server.crt /etc/openvpn/server/
openvpn --genkey tls-crypt-v2-server "$TLS_CRYPT_V2_SERVER_KEY"

cat >/etc/openvpn/server/server.conf <<EOF
port ${SERVER_PORT}
proto udp
dev tun
ca ca.crt
cert server.crt
key server.key
dh dh.pem
server ${SERVER_NETWORK}
ifconfig-pool-persist ipp.txt
push "redirect-gateway def1 bypass-dhcp"
push "dhcp-option DNS 8.8.8.8"
keepalive 10 120
tls-crypt-v2 ${TLS_CRYPT_V2_SERVER_KEY}
cipher AES-256-GCM
user nobody
group nogroup
persist-key
persist-tun
status ${STATUS_LOG}
verb 3
EOF

MAIN_INTERFACE=$(ip -4 route ls | grep default | grep -Po '(?<=dev )(\S+)' | head -1)
iptables -t nat -C POSTROUTING -s ${SERVER_NETWORK%% *}/24 -o $MAIN_INTERFACE -j MASQUERADE 2>/dev/null || \
iptables -t nat -A POSTROUTING -s ${SERVER_NETWORK%% *}/24 -o $MAIN_INTERFACE -j MASQUERADE
apt-get install -y iptables-persistent
netfilter-persistent save
sysctl -w net.ipv4.ip_forward=1
grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

systemctl enable --now openvpn-server@server

# ------------------------------------------------------------------
# 4. MariaDB
# ------------------------------------------------------------------
echo "==> Configuring MariaDB..."
systemctl enable --now mariadb
mysql -e "CREATE DATABASE IF NOT EXISTS \`${MYSQL_DB}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -e "CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';"
mysql -e "GRANT ALL PRIVILEGES ON \`${MYSQL_DB}\`.* TO '${MYSQL_USER}'@'localhost'; FLUSH PRIVILEGES;"

# ------------------------------------------------------------------
# 5. Python virtual environment
# ------------------------------------------------------------------
echo "==> Creating Python venv..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install fastapi "uvicorn[standard]" python-dotenv mysql-connector-python requests
deactivate

# ------------------------------------------------------------------
# 6. Environment file
# ------------------------------------------------------------------
cat >/etc/ovpn-service.env <<EOF
API_TOKEN=${API_TOKEN}
API_PORT=${API_PORT}
MYSQL_HOST=127.0.0.1
MYSQL_DB=${MYSQL_DB}
MYSQL_USER=${MYSQL_USER}
MYSQL_PASSWORD=${MYSQL_PASSWORD}
SERVER_ENDPOINT_IP=${PUBLIC_IP}
SERVER_ENDPOINT_PORT=${SERVER_PORT}
EASYRSA_DIR=${EASYRSA_DIR}
TLS_CRYPT_V2_SERVER_KEY=${TLS_CRYPT_V2_SERVER_KEY}
CLIENT_KEYS_DIR=${CLIENT_KEYS_DIR}
STATUS_LOG=${STATUS_LOG}
EOF
chmod 600 /etc/ovpn-service.env

# ------------------------------------------------------------------
# 7. systemd service
# ------------------------------------------------------------------
SERVICE_FILE=/etc/systemd/system/ovpn-service.service
cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=OpenVPN Profile API (via virtualenv)
After=network.target mariadb.service openvpn-server@server.service
Requires=openvpn-server@server.service

[Service]
Type=simple
EnvironmentFile=/etc/ovpn-service.env
ExecStart=${VENV_DIR}/bin/uvicorn ovpn_service:app --host 0.0.0.0 --port \${API_PORT} --workers ${WORKERS}
WorkingDirectory=/opt
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable --now ovpn-service.service

# ------------------------------------------------------------------
# 8. Firewall
# ------------------------------------------------------------------
if command -v ufw >/dev/null; then
  ufw allow ${SERVER_PORT}/udp || true
fi

# ------------------------------------------------------------------
# 9. Summary
# ------------------------------------------------------------------
echo "------------------------------------------------------------"
echo "✅  Installation complete."
echo "   OpenVPN port: ${SERVER_PORT}/udp"
echo "   API listens: http://${PUBLIC_IP}:${API_PORT}"
echo "   Token: ${API_TOKEN}"
echo
echo "   Example request to create a profile:"
echo "     curl -X POST \"http://${PUBLIC_IP}:${API_PORT}/profiles?token=${API_TOKEN}\""
echo
echo "   Logs: journalctl -u ovpn-service -f"
echo "------------------------------------------------------------"
