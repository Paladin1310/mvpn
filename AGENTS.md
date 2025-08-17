install.sh - запускается на полностью новой ubuntu 24.04 на vps сервере.
команда которой идет запуск:
bash -c 'command -v fzf >/dev/null 2>&1 || { sudo apt-get update && sudo apt-get install -y fzf; }; BRANCH=$(git ls-remote --heads https://github.com/Paladin1310/mvpn.git | sed "s?.*refs/heads/??" | fzf --prompt="Выберите ветку> "); cd /opt && for f in wg_service.py install.sh; do curl -L https://raw.githubusercontent.com/Paladin1310/mvpn/$BRANCH/$f -o $f; done && chmod +x install.sh && ./install.sh'

### установить, удалить или обновить

bash -c '
set -e
need(){ command -v "$1" >/dev/null 2>&1 || PKGS="$PKGS $1"; }
PKGS=""
need git; need curl; need fzf
[ -n "$PKGS" ] && { sudo apt-get update && sudo apt-get install -y $PKGS; }

REPO="https://github.com/Paladin1310/mvpn.git"
ACTION=$(printf "install\nuninstall\nupdate" | fzf --prompt="Выберите действие> " --height=10 --border) || { echo "Отменено"; exit 1; }

if [ "$ACTION" = "install" ]; then
  BRANCH_INSTALL=$(git ls-remote --heads "$REPO" | sed "s?.*refs/heads/??" | fzf --prompt="Ветка для установки> ") || { echo "Отменено"; exit 1; }
  cd /opt
  for f in wg_service.py install.sh; do curl -fsSL https://raw.githubusercontent.com/Paladin1310/mvpn/$BRANCH_INSTALL/$f -o "$f"; done
  chmod +x install.sh && ./install.sh

elif [ "$ACTION" = "uninstall" ]; then
  BRANCH_UNINSTALL=$(git ls-remote --heads "$REPO" | sed "s?.*refs/heads/??" | fzf --prompt="Ветка для удаления> ") || { echo "Отменено"; exit 1; }
  cd /opt
  curl -fsSL https://raw.githubusercontent.com/Paladin1310/mvpn/$BRANCH_UNINSTALL/uninstall.sh -o uninstall.sh
  chmod +x uninstall.sh && ./uninstall.sh

else
  BRANCH_UNINSTALL=$(git ls-remote --heads "$REPO" | sed "s?.*refs/heads/??" | fzf --prompt="Ветка для удаления (uninstall)> ") || { echo "Отменено"; exit 1; }
  cd /opt
  curl -fsSL https://raw.githubusercontent.com/Paladin1310/mvpn/$BRANCH_UNINSTALL/uninstall.sh -o uninstall.sh
  chmod +x uninstall.sh && ./uninstall.sh
  BRANCH_INSTALL=$(git ls-remote --heads "$REPO" | sed "s?.*refs/heads/??" | fzf --prompt="Ветка для установки (install)> ") || { echo "Отменено"; exit 1; }
  for f in wg_service.py install.sh; do curl -fsSL https://raw.githubusercontent.com/Paladin1310/mvpn/$BRANCH_INSTALL/$f -o "$f"; done
  chmod +x install.sh && ./install.sh
fi
'
