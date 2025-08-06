install.sh - запускается на полностью новой ubuntu 24.04 на vps сервере.
команда которой идет запуск:
bash -c 'command -v fzf >/dev/null 2>&1 || { sudo apt-get update && sudo apt-get install -y fzf; }; BRANCH=$(git ls-remote --heads https://github.com/Paladin1310/mvpn.git | sed "s?.*refs/heads/??" | fzf --prompt="Выберите ветку> "); cd /opt && for f in awg_service.py install.sh; do curl -L https://raw.githubusercontent.com/Paladin1310/mvpn/$BRANCH/$f -o $f; done && chmod +x install.sh && ./install.sh'
