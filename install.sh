#!/usr/bin/env bash
# install_xray_service.sh — быстрый деплой /opt/wg_service.py + Xray VLESS+Reality на Ubuntu 24.04

set -euo pipefail

# ------------------------------------------------------------------
# 1. Проверки и переменные
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

SERVER_ADDRESS="${1:-}"
if [[ -z "$SERVER_ADDRESS" ]]; then
  SERVER_ADDRESS=$(curl -s https://api.ipify.org || true)
  [[ -z "$SERVER_ADDRESS" ]] && {
    echo "Укажите внешний IP/домен: ./install.sh <ADDRESS>" >&2
    exit 1
  }
fi

API_TOKEN=$(openssl rand -hex 32)
MYSQL_PASSWORD=$(openssl rand -hex 16)
MYSQL_USER="xray_user"
MYSQL_DB="xray_panel"
MYSQL_TEMP_DB="xray_temp"
API_PORT="8080"
XRAY_PORT="443"
VENV_DIR="/opt/xray_service_venv"
WORKERS=$(( $(nproc) * 2 ))
XRAY_CONFIG="/usr/local/etc/xray/config.json"

# ------------------------------------------------------------------
# 2. Системные зависимости
# ------------------------------------------------------------------
echo "==> Устанавливаю системные пакеты…"
apt update -y
apt install -y --no-install-recommends python3-venv python3-pip mariadb-server curl jq

if ! command -v xray >/dev/null 2>&1; then
  echo "==> Устанавливаю Xray…"
  bash <(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh) install
fi

# ------------------------------------------------------------------
# 3. Генерация ключей Reality и базовый конфиг Xray
# ------------------------------------------------------------------
echo "==> Настраиваю Xray…"
KEYS=$(xray x25519)
XRAY_PRIV_KEY=$(echo "$KEYS" | awk '/Private key/ {print $3}')
XRAY_PUB_KEY=$(echo "$KEYS" | awk '/Public key/ {print $3}')

mkdir -p $(dirname "$XRAY_CONFIG")
cat >"$XRAY_CONFIG" <<'CONFIG'
{
  "log": {"loglevel": "warning"},
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": XRAY_PORT_REPLACE,
      "protocol": "vless",
      "settings": {
        "clients": [],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "dest": "vk.com:443",
          "serverNames": ["vk.com"],
          "privateKey": "XRAY_PRIVKEY_REPLACE",
          "shortIds": []
        }
      }
    }
  ],
  "outbounds": [{"protocol": "freedom"}]
}
CONFIG

sed -i "s/XRAY_PORT_REPLACE/$XRAY_PORT/" "$XRAY_CONFIG"
sed -i "s/XRAY_PRIVKEY_REPLACE/$XRAY_PRIV_KEY/" "$XRAY_CONFIG"

systemctl enable --now xray
systemctl restart xray

# ------------------------------------------------------------------
# 4. MariaDB / MySQL
# ------------------------------------------------------------------
echo "==> Настраиваю MariaDB…"
systemctl enable --now mariadb
mysql -e "CREATE DATABASE IF NOT EXISTS \`$MYSQL_DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -e "CREATE USER IF NOT EXISTS '$MYSQL_USER'@'localhost' IDENTIFIED BY '$MYSQL_PASSWORD';"
mysql -e "GRANT ALL PRIVILEGES ON \`$MYSQL_DB\`.* TO '$MYSQL_USER'@'localhost'; FLUSH PRIVILEGES;"
# Temp DB (отдельная база для временных профилей)
mysql -e "CREATE DATABASE IF NOT EXISTS \`$MYSQL_TEMP_DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -e "GRANT ALL PRIVILEGES ON \`$MYSQL_TEMP_DB\`.* TO '$MYSQL_USER'@'localhost'; FLUSH PRIVILEGES;"

# ------------------------------------------------------------------
# 5. Python-виртуальное окружение
# ------------------------------------------------------------------
echo "==> Создаю Python-venv…"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install fastapi "uvicorn[standard]" python-dotenv mysql-connector-python requests psutil
deactivate

# ------------------------------------------------------------------
# 6. Env-файл для API
# ------------------------------------------------------------------
cat >/etc/wg-service.env <<'ENV'
API_TOKEN=API_TOKEN_REPLACE
API_PORT=API_PORT_REPLACE
SERVER_DOMAIN=SERVER_ADDRESS_REPLACE
SERVER_PORT=XRAY_PORT_REPLACE
SERVER_PUBLIC_KEY=XRAY_PUBKEY_REPLACE
XRAY_CONFIG=XRAY_CONFIG_REPLACE

MYSQL_HOST=127.0.0.1
MYSQL_DB=MYSQL_DB_REPLACE
MYSQL_USER=MYSQL_USER_REPLACE
MYSQL_PASSWORD=MYSQL_PASSWORD_REPLACE

# Separate DB for temporary profiles (опционально можно переопределить)
TEMP_MYSQL_HOST=127.0.0.1
TEMP_MYSQL_DB=MYSQL_TEMP_DB_REPLACE
TEMP_MYSQL_USER=MYSQL_USER_REPLACE
TEMP_MYSQL_PASSWORD=MYSQL_PASSWORD_REPLACE
ENV
sed -i "s/API_TOKEN_REPLACE/$API_TOKEN/" /etc/wg-service.env
sed -i "s/API_PORT_REPLACE/$API_PORT/" /etc/wg-service.env
sed -i "s/SERVER_ADDRESS_REPLACE/$SERVER_ADDRESS/" /etc/wg-service.env
sed -i "s/XRAY_PORT_REPLACE/$XRAY_PORT/" /etc/wg-service.env
sed -i "s/XRAY_PUBKEY_REPLACE/$XRAY_PUB_KEY/" /etc/wg-service.env
sed -i "s|XRAY_CONFIG_REPLACE|$XRAY_CONFIG|" /etc/wg-service.env
sed -i "s/MYSQL_DB_REPLACE/$MYSQL_DB/" /etc/wg-service.env
sed -i "s/MYSQL_USER_REPLACE/$MYSQL_USER/" /etc/wg-service.env
sed -i "s/MYSQL_PASSWORD_REPLACE/$MYSQL_PASSWORD/" /etc/wg-service.env
sed -i "s/MYSQL_TEMP_DB_REPLACE/$MYSQL_TEMP_DB/" /etc/wg-service.env || true
chmod 600 /etc/wg-service.env

# ------------------------------------------------------------------
# 7. systemd-юнит для FastAPI
# ------------------------------------------------------------------
SERVICE_UNIT=/etc/systemd/system/wg-service.service
cat >"$SERVICE_UNIT" <<'UNIT'
[Unit]
Description=Xray VLESS Profile API (via virtualenv)
After=network.target mariadb.service xray.service

[Service]
Type=simple
EnvironmentFile=/etc/wg-service.env
ExecStart=VENV_DIR_REPLACE/bin/uvicorn wg_service:app --host 0.0.0.0 --port ${API_PORT} --workers WORKERS_REPLACE
WorkingDirectory=/opt
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sed -i "s|VENV_DIR_REPLACE|$VENV_DIR|" "$SERVICE_UNIT"
sed -i "s/WORKERS_REPLACE/$WORKERS/" "$SERVICE_UNIT"

chmod 644 "$SERVICE_UNIT"
systemctl daemon-reload
systemctl enable --now wg-service.service

# ------------------------------------------------------------------
# 8. Фаервол (UFW) — опционально
# ------------------------------------------------------------------
if command -v ufw >/dev/null; then
  ufw allow $XRAY_PORT/tcp || true
fi

# ------------------------------------------------------------------
# 9. Итог
# ------------------------------------------------------------------
cat <<INFO
------------------------------------------------------------
✅  Установка завершена.
   Xray VLESS Reality порт: $XRAY_PORT/tcp
   API слушает: http://$SERVER_ADDRESS:$API_PORT
   Токен: $API_TOKEN

   Пример запроса (создать профиль):
     curl -X POST "http://$SERVER_ADDRESS:$API_PORT/profiles?token=$API_TOKEN"

   Временный профиль (1 день):
     curl -X POST "http://$SERVER_ADDRESS:$API_PORT/temp-profiles?token=$API_TOKEN"

   Логи: journalctl -у wg-service -ф
------------------------------------------------------------
INFO
