#!/usr/bin/env bash
set -euo pipefail

# ========= ПАРАМЕТРЫ =========
SERVICE_NAME="ble-tty.service"
INSTALL_DIR="/opt/ble-tty"
VENV_DIR="${INSTALL_DIR}/venv"
SRC_PY_REL="ble_tty.py"   # скрипт сервиса в твоём репозитории
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_PY_ABS="${SRC_DIR}/${SRC_PY_REL}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
BT_OVERRIDE_DIR="/etc/systemd/system/bluetooth.service.d"
BT_OVERRIDE_FILE="${BT_OVERRIDE_DIR}/override.conf"
LOCAL_NAME_DEFAULT="RPi-BLE-UART"     # только инфо-текст; имя устройства задаётся внутри python-скрипта

# ========= ПРОВЕРКИ =========
if [[ $EUID -ne 0 ]]; then
  echo "Перезапусти: sudo $0"
  exit 1
fi

if [[ ! -f "${SRC_PY_ABS}" ]]; then
  echo "Не найден файл приложения: ${SRC_PY_ABS}"
  echo "Убедись, что ${SRC_PY_REL} находится в репозитории рядом со start.sh"
  exit 1
fi

changed_flag=0

# ========= APT БАЗА =========
echo "[*] Установка базовых пакетов APT..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  bluetooth bluez bluez-tools python3 python3-venv python3-pip

# ========= bluetoothd --experimental (автоопределение пути) =========
echo "[*] Включение bluetoothd --experimental..."
mkdir -p "${BT_OVERRIDE_DIR}"

BTD_BIN=""
for p in /usr/libexec/bluetooth/bluetoothd /usr/lib/bluetooth/bluetoothd $(command -v bluetoothd 2>/dev/null || true); do
  if [[ -n "${p}" && -x "${p}" ]]; then
    BTD_BIN="${p}"
    break
  fi
done
if [[ -z "${BTD_BIN}" ]]; then
  echo "[-] Не найден bluetoothd. Проверь установку пакета bluez."
  exit 1
fi

BT_UNIT_CONTENT=$"[Service]\nExecStart=\nExecStart=${BTD_BIN} --experimental\n"
if [[ ! -f "${BT_OVERRIDE_FILE}" ]] || ! diff -q <(echo -e "${BT_UNIT_CONTENT}") "${BT_OVERRIDE_FILE}" >/dev/null 2>&1; then
  printf "%b" "${BT_UNIT_CONTENT}" > "${BT_OVERRIDE_FILE}"
  systemctl daemon-reload
  systemctl restart bluetooth.service || {
    echo "[-] Ошибка перезапуска bluetooth.service; смотри: journalctl -xeu bluetooth.service"
    exit 1
  }
  changed_flag=1
  echo "  - Применён override: ${BTD_BIN} --experimental"
else
  echo "  - override уже актуален"
fi

# ========= РАЗВЁРТЫВАНИЕ ПРИЛОЖЕНИЯ =========
echo "[*] Развёртывание в ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}"

# Копируем/python-скрипт при изменении (по хэшу)
dest_py="${INSTALL_DIR}/$(basename "${SRC_PY_REL}")"
src_sum="$(sha256sum "${SRC_PY_ABS}" | awk '{print $1}')"
dst_sum="$( [[ -f "${dest_py}" ]] && sha256sum "${dest_py}" | awk '{print $1}' || echo "NONE" )"
if [[ "${src_sum}" != "${dst_sum}" ]]; then
  install -m 0755 "${SRC_PY_ABS}" "${dest_py}"
  changed_flag=1
  echo "  - Обновлён код: ${dest_py}"
fi

# ========= VENV + ЗАВИСИМОСТИ (только dbus-next) =========
echo "[*] Настройка Python venv и зависимостей..."
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "  - Создание venv..."
  python3 -m venv "${VENV_DIR}"
  changed_flag=1
fi
"${VENV_DIR}/bin/pip" install --upgrade pip wheel setuptools >/dev/null
if ! "${VENV_DIR}/bin/python" -c "import dbus_next" 2>/dev/null; then
  echo "  - Установка dbus-next..."
  "${VENV_DIR}/bin/pip" install dbus-next >/dev/null
  changed_flag=1
fi

# ========= SYSTEMD UNIT =========
echo "[*] Конфигурация systemd-юнита..."
read -r -d '' UNIT_CONTENT <<'EOF'
[Unit]
Description=BLE UART (Nordic UART Service) to TTY shell bridge (dbus-next)
After=bluetooth.service network.target
Requires=bluetooth.service

[Service]
Type=simple
ExecStart=__PY__ __APP__
Restart=on-failure
RestartSec=2
User=root
Environment=PYTHONUNBUFFERED=1

# Небольшая изоляция (можно ослабить при необходимости)
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

UNIT_FILLED="${UNIT_CONTENT/__PY__/${VENV_DIR//\//\\/}\\/bin\\/python}"
UNIT_FILLED="${UNIT_FILLED/__APP__/${dest_py//\//\\/}}"

if [[ ! -f "${UNIT_PATH}" ]] || ! diff -q <(printf "%b" "${UNIT_FILLED}") "${UNIT_PATH}" >/dev/null 2>&1; then
  printf "%b" "${UNIT_FILLED}" > "${UNIT_PATH}"
  systemctl daemon-reload
  changed_flag=1
  echo "  - Юнит обновлён: ${UNIT_PATH}"
fi

# ========= АВТОСТАРТ И (ПЕРЕ)ЗАПУСК =========
systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true

if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
  echo "[*] Старт сервиса ${SERVICE_NAME}..."
  systemctl start "${SERVICE_NAME}"
else
  if [[ "${changed_flag}" -eq 1 ]]; then
    echo "[*] Обнаружены изменения — перезапуск сервиса ${SERVICE_NAME}..."
    systemctl restart "${SERVICE_NAME}"
  else
    echo "[*] Изменений нет — сервис уже запущен и актуален."
  fi
fi

# ========= КРАТКАЯ ДИАГНОСТИКА =========
echo
echo "=== Статус ${SERVICE_NAME} ==="
systemctl --no-pager --full status "${SERVICE_NAME}" | sed -n '1,25p' || true
echo
echo "Готово. Подключайся к BLE имени из скрипта (напр. \"${LOCAL_NAME_DEFAULT}\") через Serial Bluetooth Terminal (BLE / Nordic UART Service)."
