#!/usr/bin/env bash
set -euo pipefail

# ==== Параметры ====
SERVICE_NAME="ble-tty.service"
INSTALL_DIR="/opt/ble-tty"
VENV_DIR="${INSTALL_DIR}/venv"
SRC_PY_REL="ble_uart_shell.py"     # путь к скрипту в репо (рядом с этим инсталлером)
SRC_PY_ABS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${SRC_PY_REL}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
BT_OVERRIDE_DIR="/etc/systemd/system/bluetooth.service.d"
BT_OVERRIDE_FILE="${BT_OVERRIDE_DIR}/override.conf"

# ==== Проверки ====
if [[ $EUID -ne 0 ]]; then
  echo "Перезапусти: sudo $0"
  exit 1
fi

if [[ ! -f "${SRC_PY_ABS}" ]]; then
  echo "Не найден файл приложения: ${SRC_PY_ABS}"
  echo "Убедись, что ble_uart_shell.py находится в репозитории рядом со скриптом."
  exit 1
fi

changed_flag=0  # если что-то поменялось — перезапустим сервис

# ==== Пакеты APT ====
echo "[*] Установка зависимостей APT (bluetooth / bluez / python3-venv)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  bluetooth bluez bluez-tools \
  python3 python3-venv python3-pip

# ==== Включаем bluetoothd --experimental для GATT-периферии ====
echo "[*] Включение bluetoothd --experimental..."
mkdir -p "${BT_OVERRIDE_DIR}"
BT_UNIT_CONTENT=$'[Service]\nExecStart=\nExecStart=/usr/lib/bluetooth/bluetoothd --experimental\n'
if [[ ! -f "${BT_OVERRIDE_FILE}" ]] || ! diff -q <(echo -e "${BT_UNIT_CONTENT}") "${BT_OVERRIDE_FILE}" >/dev/null 2>&1; then
  printf "%b" "${BT_UNIT_CONTENT}" > "${BT_OVERRIDE_FILE}"
  systemctl daemon-reload
  systemctl restart bluetooth.service
  changed_flag=1
  echo "  - Применён override для bluetooth.service"
fi

# ==== Развёртывание каталога приложения ====
echo "[*] Развёртывание в ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}"

# virtualenv
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "  - Создание venv..."
  python3 -m venv "${VENV_DIR}"
  changed_flag=1
fi

# Обновляем pip/setuptools/wheel и ставим/обновляем bluezero
echo "[*] Установка/обновление python-зависимостей..."
"${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel >/dev/null
# pin можно не ставить; при желании зафиксируй версию:
# "${VENV_DIR}/bin/pip" install bluezero==0.8.0
"${VENV_DIR}/bin/pip" install bluezero >/dev/null

# Копируем приложение, если изменилось (по хэшу)
echo "[*] Обновление приложения..."
dest_py="${INSTALL_DIR}/ble_uart_shell.py"
src_sum="$(sha256sum "${SRC_PY_ABS}" | awk '{print $1}')"
dst_sum="$( [[ -f "${dest_py}" ]] && sha256sum "${dest_py}" | awk '{print $1}' || echo "NONE" )"
if [[ "${src_sum}" != "${dst_sum}" ]]; then
  install -m 0755 "${SRC_PY_ABS}" "${dest_py}"
  changed_flag=1
  echo "  - Код обновлён"
fi

# ==== systemd unit ====
echo "[*] Конфигурация systemd-юнита..."
read -r -d '' UNIT_CONTENT <<'EOF'
[Unit]
Description=BLE UART (Nordic UART Service) to TTY shell bridge
After=bluetooth.service network.target
Requires=bluetooth.service

[Service]
Type=simple
# ВАЖНО: используем python из venv
ExecStart=__PY__ __APP__
Restart=on-failure
RestartSec=2
User=root
Environment=PYTHONUNBUFFERED=1

# Защита (можно ослабить при необходимости)
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

UNIT_CONTENT="${UNIT_CONTENT/__PY__/${VENV_DIR//\//\\/}\\/bin\\/python}"
UNIT_CONTENT="${UNIT_CONTENT/__APP__/${dest_py//\//\\/}}"

if [[ ! -f "${UNIT_PATH}" ]] || ! diff -q <(printf "%b" "${UNIT_CONTENT}") "${UNIT_PATH}" >/dev/null 2>&1; then
  printf "%b" "${UNIT_CONTENT}" > "${UNIT_PATH}"
  systemctl daemon-reload
  changed_flag=1
  echo "  - Юнит обновлён"
fi

# ==== Автозапуск и (пере)запуск ====
systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true

if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
  echo "[*] Старт сервиса ${SERVICE_NAME}..."
  systemctl start "${SERVICE_NAME}"
  systemctl --no-pager --full status "${SERVICE_NAME}" || true
  exit 0
fi

if [[ "${changed_flag}" -eq 1 ]]; then
  echo "[*] Обнаружены изменения — перезапуск сервиса ${SERVICE_NAME}..."
  systemctl restart "${SERVICE_NAME}"
else
  echo "[*] Изменений нет — сервис уже запущен и актуален."
fi

# ==== Краткая диагностика ====
echo
echo "=== Статус ==="
systemctl --no-pager --full status "${SERVICE_NAME}" | sed -n '1,25p' || true
echo
echo "Готово."
