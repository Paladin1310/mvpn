#!/usr/bin/env bash
# install_xray_service.sh — быстрый деплой /opt/wg_service.py + Xray (VLESS+Reality)
# на чистую Ubuntu 24.04

set -euo pipefail

# ------------------------------------------------------------------
# 1. Проверки и основные переменные
# ------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  echo "Пожалуйста, запускайте скрипт от root." >&2
  exit 1
fi

SERVICE_FILE="/opt/wg_service.py"
if [[ ! -f "$SERVICE_FILE" ]]; then
  echo "Файл $SERVICE_FILE не найден. Скопируйте его перед запуском." >&2
  exit 1
fi

DOMAIN_OR_IP="${1:-}"
if [[ -z "$DOMAIN_OR_IP" ]]; then
  DOMAIN_OR_IP=$(curl -s https://api.ipify.org || true)
  [[ -z "$DOMAIN_OR_IP" ]] && {
    echo "Укажите домен или внешний IP: ./install.sh <DOMAIN_OR_IP>" >&2
    exit 1
  }
fi

SNI="${2:-$DOMAIN_OR_IP}"
XRAY_PORT="${3:-443}"

# Генерация секретов
API_TOKEN=$(openssl rand -hex 32)
MYSQL_PASSWORD=$(openssl rand -hex 16)
MYSQL_USER="xray_user"
MYSQL_DB="xray_panel"
API_PORT="8080"
WORKERS=$(( $(nproc) * 2 ))
VENV_DIR="/opt/xray_service_venv"
XRAY_CONFIG="/usr/local/etc/xray/config.json"

# Reality keys и short id
REALITY_PRIV=$(xray x25519 | grep Private | awk '{print $3}')
REALITY_PUB=$(xray x25519 | grep Public | awk '{print $3}')
SHORT_ID=$(openssl rand -hex 8)

# ------------------------------------------------------------------
# 2. Системные зависимости
# ------------------------------------------------------------------
echo "==> Устанавливаю системные пакеты…"
apt update -y
apt install -y --no-install-recommends curl python3-venv python3-pip mariadb-server gnupg2

echo "==> Устанавливаю Xray…"
curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh | bash -s -- install

# ------------------------------------------------------------------
# 3. Настройка Xray (VLESS+Reality)
# ------------------------------------------------------------------
echo "==> Настраиваю Xray…"
mkdir -p /usr/local/etc/xray
cat >"$XRAY_CONFIG" <<EOF
{
  "inbounds": [
    {
      "port": $XRAY_PORT,
      "protocol": "vless",
      "settings": {
        "clients": [],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "www.cloudflare.com:443",
          "xver": 0,
          "serverNames": ["$DOMAIN_OR_IP"],
          "privateKey": "$REALITY_PRIV",
          "shortIds": ["$SHORT_ID"]
        }
      }
    }
  ],
  "outbounds": [
    { "protocol": "freedom" },
    { "protocol": "blackhole", "tag": "blocked" }
  ]
}
EOF

systemctl enable --now xray

# ------------------------------------------------------------------
# 4. MariaDB / MySQL
# ------------------------------------------------------------------
echo "==> Настраиваю MariaDB…"
systemctl enable --now mariadb
mysql -e "CREATE DATABASE IF NOT EXISTS \`${MYSQL_DB}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -e "CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';"
mysql -e "GRANT ALL PRIVILEGES ON \`${MYSQL_DB}\`.* TO '${MYSQL_USER}'@'localhost'; FLUSH PRIVILEGES;"

# ------------------------------------------------------------------
# 5. Python-виртуальное окружение
# ------------------------------------------------------------------
echo "==> Создаю Python-venv…"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install fastapi "uvicorn[standard]" python-dotenv mysql-connector-python
deactivate

# ------------------------------------------------------------------
# 6. Env-файл для API
# ------------------------------------------------------------------
cat >/etc/wg-service.env <<EOF
API_TOKEN=${API_TOKEN}
API_PORT=${API_PORT}

# Xray
XRAY_CONFIG=${XRAY_CONFIG}
XRAY_DOMAIN=${DOMAIN_OR_IP}
XRAY_PORT=${XRAY_PORT}
XRAY_PUBLIC_KEY=${REALITY_PUB}
XRAY_SNI=${SNI}
XRAY_SHORT_ID=${SHORT_ID}

# MySQL
MYSQL_HOST=127.0.0.1
MYSQL_DB=${MYSQL_DB}
MYSQL_USER=${MYSQL_USER}
MYSQL_PASSWORD=${MYSQL_PASSWORD}
EOF
chmod 600 /etc/wg-service.env

# ------------------------------------------------------------------
# 7. systemd-юнит для FastAPI
# ------------------------------------------------------------------
SERVICE_UNIT=/etc/systemd/system/wg-service.service
cat >"$SERVICE_UNIT" <<EOF
[Unit]
Description=Xray VLESS API (via virtualenv)
After=network.target mariadb.service xray.service

[Service]
Type=simple
EnvironmentFile=/etc/wg-service.env
ExecStart=${VENV_DIR}/bin/uvicorn wg_service:app --host 0.0.0.0 --port \${API_PORT} --workers ${WORKERS}
WorkingDirectory=/opt
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$SERVICE_UNIT"
systemctl daemon-reload
systemctl enable --now wg-service.service

# ------------------------------------------------------------------
# 8. UFW (если установлен)
# ------------------------------------------------------------------
if command -v ufw >/dev/null; then
  ufw allow ${XRAY_PORT}/tcp || true
fi

# ------------------------------------------------------------------
# 9. Итоги
# ------------------------------------------------------------------
echo "------------------------------------------------------------"
echo "✅  Установка завершена"
echo "   Xray слушает: tcp://${DOMAIN_OR_IP}:${XRAY_PORT}" 
echo "   Reality public key: ${REALITY_PUB}"
echo "   Short ID: ${SHORT_ID}"
echo "   API слушает: http://${DOMAIN_OR_IP}:${API_PORT}"
echo "   Токен: ${API_TOKEN}"
echo
echo "   Пример запроса (создать клиента):"
echo "     curl -X POST \"http://${DOMAIN_OR_IP}:${API_PORT}/clients?token=${API_TOKEN}&label=test\""
echo
echo "   Логи: journalctl -u wg-service -f"
echo "------------------------------------------------------------"

