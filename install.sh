#!/usr/bin/env bash
# install.sh — быстрый деплой Xray (VLESS+Reality) и FastAPI-сервиса управления клиентами
# Работает на свежей Ubuntu 24.04

set -euo pipefail

# ------------------------------------------------------------------
# 1. Проверки и переменные
# ------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  echo "Пожалуйста, запускайте скрипт от root." >&2
  exit 1
fi

SERVICE_FILE_ON_DISK="/opt/wg_service.py"
if [[ ! -f "$SERVICE_FILE_ON_DISK" ]]; then
  echo "Файл $SERVICE_FILE_ON_DISK не найден. Скопируйте его перед запуском." >&2
  exit 1
fi

PUBLIC_IP="${1:-}"
if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP=$(curl -s https://api.ipify.org || true)
  [[ -z "$PUBLIC_IP" ]] && {
    echo "Укажите публичный IP или домен: ./install.sh <PUBLIC_IP>" >&2
    exit 1
  }
fi

XRAY_PORT="443"
API_PORT="8080"
API_TOKEN=$(openssl rand -hex 32)
MYSQL_PASSWORD=$(openssl rand -hex 16)
MYSQL_USER="xray_user"
MYSQL_DB="xray_panel"
WORKERS=$(( $(nproc) * 2 ))
VENV_DIR="/opt/wg_service_venv"

# ------------------------------------------------------------------
# 2. Системные зависимости
# ------------------------------------------------------------------
echo "==> Устанавливаю системные пакеты…"
apt update -y
apt install -y --no-install-recommends python3-venv python3-pip mariadb-server curl unzip

# ------------------------------------------------------------------
# 3. Установка Xray Core
# ------------------------------------------------------------------
echo "==> Устанавливаю Xray…"
curl -Ls https://github.com/XTLS/Xray-install/raw/main/install-release.sh -o /tmp/xray-install.sh
bash /tmp/xray-install.sh install >/dev/null

# Генерируем ключи Reality
KEYS=$(/usr/local/bin/xray x25519)
XRAY_PRIVATE_KEY=$(echo "$KEYS" | sed -n 's/^Private key: //p')
XRAY_PUBLIC_KEY=$(echo "$KEYS" | sed -n 's/^Public key: //p')

# Базовый конфиг Xray
mkdir -p /usr/local/etc/xray
cat >/usr/local/etc/xray/config.json <<EOF
{
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": ${XRAY_PORT},
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
          "serverNames": ["vk.com"],
          "privateKey": "${XRAY_PRIVATE_KEY}",
          "shortIds": []
        }
      }
    }
  ]
}
EOF

systemctl enable xray
systemctl restart xray

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
pip install fastapi "uvicorn[standard]" python-dotenv mysql-connector-python requests
deactivate

# ------------------------------------------------------------------
# 6. Env-файл для API
# ------------------------------------------------------------------
cat >/etc/xray-service.env <<EOF
API_TOKEN=${API_TOKEN}
API_PORT=${API_PORT}
XRAY_CONFIG_PATH=/usr/local/etc/xray/config.json
XRAY_BINARY=/usr/local/bin/xray
SERVER_PUBLIC_KEY=${XRAY_PUBLIC_KEY}
SERVER_DOMAIN=${PUBLIC_IP}
SERVER_PORT=${XRAY_PORT}
SNI=vk.com
FP=chrome

# MySQL
MYSQL_HOST=127.0.0.1
MYSQL_DB=${MYSQL_DB}
MYSQL_USER=${MYSQL_USER}
MYSQL_PASSWORD=${MYSQL_PASSWORD}
EOF
chmod 600 /etc/xray-service.env

# ------------------------------------------------------------------
# 7. systemd-юнит для FastAPI
# ------------------------------------------------------------------
SERVICE_FILE=/etc/systemd/system/xray-service.service
cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Xray VLESS Profile API (via virtualenv)
After=network.target mariadb.service xray.service
Requires=xray.service

[Service]
Type=simple
EnvironmentFile=/etc/xray-service.env
ExecStart=${VENV_DIR}/bin/uvicorn wg_service:app --host 0.0.0.0 --port \${API_PORT} --workers ${WORKERS}
WorkingDirectory=/opt
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable --now xray-service.service

# ------------------------------------------------------------------
# 8. Фаервол (UFW) — опционально
# ------------------------------------------------------------------
if command -v ufw >/dev/null; then
  ufw allow ${XRAY_PORT}/tcp || true
fi

# ------------------------------------------------------------------
# 9. Итог
# ------------------------------------------------------------------
echo "------------------------------------------------------------"
echo "✅  Установка завершена."
echo "   Xray слушает: ${PUBLIC_IP}:${XRAY_PORT} (Reality/VLESS)"
echo "   Публичный ключ: ${XRAY_PUBLIC_KEY}"
echo "   API слушает: http://${PUBLIC_IP}:${API_PORT}"
echo "   Токен: ${API_TOKEN}"
echo
echo "   Пример запроса (создать профиль):"
echo "     curl -X POST \"http://${PUBLIC_IP}:${API_PORT}/profiles?token=${API_TOKEN}\""
echo
echo "   Логи: journalctl -u xray-service -f"
echo "------------------------------------------------------------"
