install.sh - запускается на полностью новой ubuntu 24.04 на vps сервере.
команда которой идет запуск:
bash -c 'cd /opt && \
  curl -L https://raw.githubusercontent.com/Paladin1310/mvpn/main/wg_service.py -o wg_service.py && \
  curl -L https://raw.githubusercontent.com/Paladin1310/mvpn/main/install.sh   -o install.sh   && \
  chmod +x install.sh && \
  ./install.sh'
