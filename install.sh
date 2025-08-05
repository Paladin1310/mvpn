#!/usr/bin/env bash
# install_wg_service.sh — быстрый деплой /opt/wg_service.py + awg0 + MySQL на Ubuntu 24.04

set -euo pipefail

# ------------------------------------------------------------------
# 1. Проверки и переменные
# ------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  echo "Пожалуйста, запускайте скрипт от root." >&2
  exit 1
fi

WG_SERVICE_FILE="/opt/wg_service.py"
if [[ ! -f "$WG_SERVICE_FILE" ]]; then
  echo "Файл $WG_SERVICE_FILE не найден. Скопируйте его перед запуском." >&2
  exit 1
fi

PUBLIC_IP="${1:-}"
if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP=$(curl -s https://api.ipify.org || true)
  [[ -z "$PUBLIC_IP" ]] && {
    echo "Укажите внешний IP: ./install_wg_service.sh <PUBLIC_IP>" >&2
    exit 1
  }
fi

# AmneziaWG — серверные параметры
SERVER_WG_ADDR="10.100.10.1/24"   # та же /24, что и VPN_NETWORK в сервисе
SERVER_LISTEN_PORT="51820"

# Генерируем секреты / переменные окружения
API_TOKEN=$(openssl rand -hex 32)
MYSQL_PASSWORD=$(openssl rand -hex 16)
MYSQL_USER="wg_user"
MYSQL_DB="wg_panel"
WG_INTERFACE="awg0"
API_PORT="8080"
WORKERS=$(( $(nproc) * 2 ))
VENV_DIR="/opt/wg_service_venv"

# ------------------------------------------------------------------
# 2. Системные зависимости
# ------------------------------------------------------------------
echo "==> Устанавливаю системные пакеты…"
apt update -y
apt install -y --no-install-recommends \
  iproute2 python3-venv python3-pip mariadb-server curl unzip \
  git golang-go

echo "==> Устанавливаю AmneziaWG tools…"
AWG_URL="https://github.com/amnezia-vpn/amneziawg-tools/releases/latest/download/ubuntu-22.04-amneziawg-tools.zip"
TMP_DIR=$(mktemp -d)
curl -L "$AWG_URL" -o "$TMP_DIR/awgtools.zip"
unzip -q "$TMP_DIR/awgtools.zip" -d "$TMP_DIR"
install -m 755 "$TMP_DIR"/ubuntu-22.04-amneziawg-tools/awg "$TMP_DIR"/ubuntu-22.04-amneziawg-tools/awg-quick /usr/local/bin
rm -rf "$TMP_DIR"

echo "==> Собираю userspace-бинарник amneziawg-go…"
GO_TMP=$(mktemp -d)
GOBIN=/usr/local/bin GOPATH="$GO_TMP" go install github.com/amnezia-vpn/amneziawg-go@latest
rm -rf "$GO_TMP"

cat >/etc/systemd/system/awg-quick@.service <<'EOF'
[Unit]
Description=AmneziaWG tunnel %i
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=WG_QUICK_USERSPACE_IMPLEMENTATION=amneziawg-go
ExecStart=/usr/local/bin/awg-quick up %i
ExecStop=/usr/local/bin/awg-quick down %i

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

# ------------------------------------------------------------------
# 3. Настройка AmneziaWG (awg0)
# ------------------------------------------------------------------
echo "==> Настраиваю интерфейс AmneziaWG ${WG_INTERFACE}…"
# awg-quick ищет конфиги в /etc/amnezia/amneziawg по умолчанию
WG_DIR="/etc/amnezia/amneziawg"
mkdir -p "$WG_DIR"
umask 077
[[ -f "$WG_DIR/server_private.key" ]] || awg genkey | tee "$WG_DIR/server_private.key" | awg pubkey > "$WG_DIR/server_public.key"
SERVER_PRIV_KEY=$(cat "$WG_DIR/server_private.key")
SERVER_PUB_KEY=$(cat "$WG_DIR/server_public.key")

cat >"$WG_DIR/${WG_INTERFACE}.conf" <<EOF
[Interface]
Address = ${SERVER_WG_ADDR}
ListenPort = ${SERVER_LISTEN_PORT}
PrivateKey = ${SERVER_PRIV_KEY}
SaveConfig = true
EOF
# NAT для всей VPN-подсети
MAIN_INTERFACE=$(ip -4 route ls | grep default | grep -Po '(?<=dev )(\S+)' | head -1)
iptables -t nat -C POSTROUTING -s 10.100.10.0/24 -o $MAIN_INTERFACE -j MASQUERADE 2>/dev/null || \
iptables -t nat -A POSTROUTING -s 10.100.10.0/24 -o $MAIN_INTERFACE -j MASQUERADE

# сохраняем (чтобы пережило перезагрузку)
DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent
netfilter-persistent save

# Включаем форвардинг
sysctl -w net.ipv4.ip_forward=1
grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

# Поднимаем интерфейс
systemctl enable --now awg-quick@${WG_INTERFACE}

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
cat >/etc/wg-service.env <<EOF
API_TOKEN=${API_TOKEN}
WG_INTERFACE=${WG_INTERFACE}
API_PORT=${API_PORT}
WG_CLI=awg
WG_CONF_DIR=${WG_DIR}

# Сетевые данные сервера AWG (для клиентских конфигов)
SERVER_PUBLIC_KEY=${SERVER_PUB_KEY}
SERVER_ENDPOINT_IP=${PUBLIC_IP}
SERVER_ENDPOINT_PORT=${SERVER_LISTEN_PORT}

# MySQL
MYSQL_HOST=127.0.0.1
MYSQL_DB=${MYSQL_DB}
MYSQL_USER=${MYSQL_USER}
MYSQL_PASSWORD=${MYSQL_PASSWORD}
EOF
chmod 600 /etc/wg-service.env

# ------------------------------------------------------------------
# 7. systemd-юнит
# ------------------------------------------------------------------
SERVICE_FILE=/etc/systemd/system/wg-service.service
cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=AmneziaWG Profile API (via virtualenv)
After=network.target mariadb.service awg-quick@${WG_INTERFACE}.service
Requires=awg-quick@${WG_INTERFACE}.service

[Service]
Type=simple
EnvironmentFile=/etc/wg-service.env
# uvicorn запускает wg_service:app
ExecStart=${VENV_DIR}/bin/uvicorn wg_service:app --host 0.0.0.0 --port \${API_PORT} --workers ${WORKERS}
WorkingDirectory=/opt
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable --now wg-service.service

# ------------------------------------------------------------------
# 8. Фаервол (UFW) — опционально
# ------------------------------------------------------------------
if command -v ufw >/dev/null; then
  ufw allow ${SERVER_LISTEN_PORT}/udp || true
fi

# ------------------------------------------------------------------
# 9. Итог
# ------------------------------------------------------------------
echo "------------------------------------------------------------"
echo "✅  Установка завершена."
echo "   AmneziaWG интерфейс: ${WG_INTERFACE} (${SERVER_WG_ADDR}, порт ${SERVER_LISTEN_PORT}/udp)"
echo "   API слушает: http://${PUBLIC_IP}:${API_PORT}"
echo "   Токен: ${API_TOKEN}"
echo
echo "   Пример запроса (создать профиль для user_id=1):"
echo "     curl -X POST \"http://${PUBLIC_IP}:${API_PORT}/profiles?token=${API_TOKEN}&user_id=1\""
echo
echo "   Логи: journalctl -u wg-service -f"
echo "------------------------------------------------------------"
