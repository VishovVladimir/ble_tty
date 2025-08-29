# RPi BLE UART Shell Bridge

Проект для Raspberry Pi 4 (и других с BT 5.0), который поднимает **BLE-UART сервис (Nordic UART Service, NUS)** и бриджит его к локальному `bash` или реальному UART.  
Таким образом, можно подключиться с телефона через *Serial Bluetooth Terminal* (режим BLE/Nordic UART) и получить терминал Raspberry Pi «по воздуху» через BLE.

---

## Возможности

- Виртуальный BLE-UART, совместимый с *Serial Bluetooth Terminal* и другими клиентами NUS.  
- Проброс в интерактивный `bash -l` (можно легко заменить на `/dev/serial0` для работы с внешним MCU).  
- Автостарт через `systemd`.  
- Идемпотентный установочный скрипт (`install_ble_tty.sh`) — можно запускать после каждого `git pull`:  
  - ставит зависимости,  
  - настраивает `bluetoothd --experimental`,  
  - создаёт виртуальное окружение Python,  
  - копирует скрипт,  
  - устанавливает и/или перезапускает сервис.

---

## Требования

- Raspberry Pi 4 / 400 / CM4 (с модулем BT 5.0; поддержка BLE обязательна).  
- Raspberry Pi OS (Debian Bookworm или совместимый).  
- Доступ `sudo` для установки пакетов и настройки сервисов.  

---

## Установка

1. Склонировать репозиторий:
   ```bash
   git clone https://github.com/<your-user>/rpi-ble-tty.git
   cd rpi-ble-tty
   ```

2. Запустить установочный скрипт (идемпотентный, можно гонять после каждого обновления кода):
   ```bash
   sudo ./install_ble_tty.sh
   ```

3. Проверить статус:
   ```bash
   systemctl status ble-tty.service
   ```

---

## Использование

- **Android**:  
  1. Установите приложение [Serial Bluetooth Terminal](https://play.google.com/store/apps/details?id=de.kai_morich.serial_bluetooth_terminal).  
  2. Выберите *Connect → BLE devices → RPi-BLE-UART*.  
  3. Включите режим *Nordic UART Service (NUS)*.  
  4. Получите терминал Raspberry Pi прямо в приложении.

- **iOS/macOS/Linux**: используйте любой BLE-GATT клиент, поддерживающий NUS (например, *nRF Connect*).

---

## Настройка

- Имя устройства меняется в `ble_tty.py` (переменная `LOCAL_NAME`).  
- По умолчанию пробрасывается интерактивный `bash`. Чтобы переключить на физический UART (например, `/dev/serial0`):  
  - замените функцию `spawn_shell()` на открытие `serial.Serial('/dev/serial0', 115200)` и чтение/запись туда.  

---

## Производительность

- Idle: ~0.1–0.4% одного ядра, 25–40 MB RAM.  
- Подключён, активный терминал: 1–4% одного ядра, 0.2–0.3 Вт доп. потребления.  
- Скорость передачи: 5–20 kB/s (ограничение BLE-NUS).  

---

## Отладка

- Логи сервиса:
   ```bash
   journalctl -u ble-tty.service -f
   ```
- Проверка BLE-адаптера:
   ```bash
   bluetoothctl show
   ```
- Убедитесь, что `bluetoothd` запущен с `--experimental`.

---

## Лицензия

MIT (можно адаптировать под свои нужды).
