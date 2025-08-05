#!/usr/bin/env bash
# install_awg_service.sh — быстрый деплой /opt/awg_service.py + awg0 + MySQL на Ubuntu 24.04

set -euo pipefail

# ------------------------------------------------------------------
# 1. Проверки и переменные
# ------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  echo "Пожалуйста, запускайте скрипт от root." >&2
  exit 1
fi

AWG_SERVICE_FILE="/opt/awg_service.py"
if [[ ! -f "$AWG_SERVICE_FILE" ]]; then
  echo "Файл $AWG_SERVICE_FILE не найден. Скопируйте его перед запуском." >&2
  exit 1
fi

PUBLIC_IP="${1:-}"
if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP=$(curl -s https://api.ipify.org || true)
  [[ -z "$PUBLIC_IP" ]] && {
    echo "Укажите внешний IP: ./install_awg_service.sh <PUBLIC_IP>" >&2
    exit 1
  }
fi

# AmneziaWG — серверные параметры
SERVER_AWG_ADDR="10.100.10.1/24"   # та же /24, что и VPN_NETWORK в сервисе
SERVER_LISTEN_PORT="51820"

# Генерируем секреты / переменные окружения
API_TOKEN=$(openssl rand -hex 32)
MYSQL_PASSWORD=$(openssl rand -hex 16)
MYSQL_USER="awg_user"
MYSQL_DB="awg_panel"
AWG_INTERFACE="awg0"
API_PORT="8080"
WORKERS=$(( $(nproc) * 2 ))
VENV_DIR="/opt/awg_service_venv"

# Обфускационные параметры AmneziaWG (можно переопределить переменными окружения)
AWG_JC=${AWG_JC:-20}
AWG_JMIN=${AWG_JMIN:-40}
AWG_JMAX=${AWG_JMAX:-80}
AWG_S1=${AWG_S1:-40}
AWG_S2=${AWG_S2:-40}
AWG_H1=${AWG_H1:-$((0x$(openssl rand -hex 4)))}
AWG_H2=${AWG_H2:-$((0x$(openssl rand -hex 4)))}
AWG_H3=${AWG_H3:-$((0x$(openssl rand -hex 4)))}
AWG_H4=${AWG_H4:-$((0x$(openssl rand -hex 4)))}

# ------------------------------------------------------------------
# 2. Системные зависимости
# ------------------------------------------------------------------
echo "==> Устанавливаю системные пакеты…"
apt update -y
apt install -y --no-install-recommends \
  iproute2 python3-venv python3-pip mariadb-server curl unzip \
  git golang-go openssl

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
echo "==> Настраиваю интерфейс AmneziaWG ${AWG_INTERFACE}…"
# awg-quick ищет конфиги в /etc/amnezia/amneziawg по умолчанию
AWG_CONF_DIR="/etc/amnezia/amneziawg"
mkdir -p "$AWG_CONF_DIR"
umask 077
[[ -f "$AWG_CONF_DIR/server_private.key" ]] || awg genkey | tee "$AWG_CONF_DIR/server_private.key" | awg pubkey > "$AWG_CONF_DIR/server_public.key"
SERVER_PRIV_KEY=$(cat "$AWG_CONF_DIR/server_private.key")
SERVER_PUB_KEY=$(cat "$AWG_CONF_DIR/server_public.key")

cat >"$AWG_CONF_DIR/${AWG_INTERFACE}.conf" <<EOF
[Interface]
Address = ${SERVER_AWG_ADDR}
ListenPort = ${SERVER_LISTEN_PORT}
PrivateKey = ${SERVER_PRIV_KEY}
Jc = ${AWG_JC}
Jmin = ${AWG_JMIN}
Jmax = ${AWG_JMAX}
S1 = ${AWG_S1}
S2 = ${AWG_S2}
H1 = ${AWG_H1}
H2 = ${AWG_H2}
H3 = ${AWG_H3}
H4 = ${AWG_H4}
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
systemctl enable --now awg-quick@${AWG_INTERFACE}

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
cat >/etc/awg-service.env <<EOF
API_TOKEN=${API_TOKEN}
AWG_INTERFACE=${AWG_INTERFACE}
API_PORT=${API_PORT}
AWG_CLI=awg
AWG_CONF_DIR=${AWG_CONF_DIR}

# Сетевые данные сервера AWG (для клиентских конфигов)
SERVER_PUBLIC_KEY=${SERVER_PUB_KEY}
SERVER_ENDPOINT_IP=${PUBLIC_IP}
SERVER_ENDPOINT_PORT=${SERVER_LISTEN_PORT}

# Параметры обфускации
AWG_JC=${AWG_JC}
AWG_JMIN=${AWG_JMIN}
AWG_JMAX=${AWG_JMAX}
AWG_S1=${AWG_S1}
AWG_S2=${AWG_S2}
AWG_H1=${AWG_H1}
AWG_H2=${AWG_H2}
AWG_H3=${AWG_H3}
AWG_H4=${AWG_H4}

# MySQL
MYSQL_HOST=127.0.0.1
MYSQL_DB=${MYSQL_DB}
MYSQL_USER=${MYSQL_USER}
MYSQL_PASSWORD=${MYSQL_PASSWORD}
EOF
chmod 600 /etc/awg-service.env

# ------------------------------------------------------------------
# 7. systemd-юнит
# ------------------------------------------------------------------
SERVICE_FILE=/etc/systemd/system/awg-service.service
cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=AmneziaWG Profile API (via virtualenv)
After=network.target mariadb.service awg-quick@${AWG_INTERFACE}.service
Requires=awg-quick@${AWG_INTERFACE}.service

[Service]
Type=simple
EnvironmentFile=/etc/awg-service.env
# uvicorn запускает awg_service:app
ExecStart=${VENV_DIR}/bin/uvicorn awg_service:app --host 0.0.0.0 --port \${API_PORT} --workers ${WORKERS}
WorkingDirectory=/opt
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable --now awg-service.service

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
echo "   AmneziaWG интерфейс: ${AWG_INTERFACE} (${SERVER_AWG_ADDR}, порт ${SERVER_LISTEN_PORT}/udp)"
echo "   API слушает: http://${PUBLIC_IP}:${API_PORT}"
echo "   Токен: ${API_TOKEN}"
echo
echo "   Пример запроса (создать профиль для user_id=1):"
echo "     curl -X POST \"http://${PUBLIC_IP}:${API_PORT}/profiles?token=${API_TOKEN}&user_id=1\""
echo
echo "   Логи: journalctl -u awg-service -f"
echo "------------------------------------------------------------"
