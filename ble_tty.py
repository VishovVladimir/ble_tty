#!/usr/bin/env python3
import os, pty, fcntl, termios, tty, select, sys, signal, time
from bluezero import peripheral

# Nordic UART Service UUIDs (поддерживается Serial Bluetooth Terminal - BLE)
UART_UUID = '6e400001-b5a3-f393-e0a9-e50e24dcca9e'
TX_UUID   = '6e400003-b5a3-f393-e0a9-e50e24dcca9e'  # notify: RPi -> phone
RX_UUID   = '6e400002-b5a3-f393-e0a9-e50e24dcca9e'  # write:  phone -> RPi

LOCAL_NAME = 'RPi-BLE-UART'   # видно в сканере BLE
MAX_CHUNK  = 180              # безопасно для MTU≈247 (перестрахуемся)

class ShellBridge:
    def __init__(self):
        self.master_fd = None
        self.child_pid = None
        self.tx_char = None
        self.client_subscribed = False

    def spawn_shell(self):
        master, slave = pty.openpty()
        # Настроим терминал "как у человека":
        for fd in (master, slave):
            attrs = termios.tcgetattr(fd)
            attrs[3] = attrs[3] | termios.ECHO | termios.ICANON
            termios.tcsetattr(fd, termios.TCSANOW, attrs)

        pid = os.fork()
        if pid == 0:
            # Дочерний: привязываем slave к stdin/out/err и запускаем bash -l
            os.setsid()
            os.close(master)
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            if slave > 2:
                os.close(slave)
            # Логин-шелл
            os.execvp('/bin/bash', ['/bin/bash', '-l'])
        else:
            # Родитель
            os.close(slave)
            # Неблокирующий master
            fl = fcntl.fcntl(master, fcntl.F_GETFL)
            fcntl.fcntl(master, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            self.master_fd = master
            self.child_pid = pid

    def write_to_shell(self, data: bytes):
        if self.master_fd is not None:
            os.write(self.master_fd, data)

    def read_from_shell(self) -> bytes:
        if self.master_fd is None:
            return b''
        r, _, _ = select.select([self.master_fd], [], [], 0)
        if self.master_fd in r:
            try:
                return os.read(self.master_fd, 4096)
            except OSError:
                return b''
        return b''

    def kill_shell(self):
        if self.child_pid:
            try:
                os.kill(self.child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

bridge = ShellBridge()

def rx_handler(value: bytes):
    # Из телефона в шелл; добавим перевод строки, если не было
    if value:
        bridge.write_to_shell(value)

def tx_notifier():
    # Читаем из шелла и пушим в BLE нотификациями
    if not bridge.client_subscribed or not bridge.tx_char:
        return
    data = bridge.read_from_shell()
    if not data:
        return
    # Режем на куски под MTU
    start = 0
    ln = len(data)
    while start < ln:
        chunk = data[start:start+MAX_CHUNK]
        # notify_value принимает bytes/bytearray
        try:
            bridge.tx_char.set_value(bytes(chunk))
            bridge.tx_char.notify()
        except Exception:
            # Клиент мог отписаться/отвалиться
            pass
        start += MAX_CHUNK

def main():
    # Поднимаем шелл
    bridge.spawn_shell()

    # Описываем GATT-сервис NUS
    nus = peripheral.Service(UART_UUID)

    # RX: write (phone -> RPi)
    rx_char = peripheral.Characteristic(
        uuid=RX_UUID,
        properties=['write', 'write-without-response'],
        value=None,
        write_callback=lambda value, options: rx_handler(value)
    )

    # TX: notify (RPi -> phone)
    tx_char = peripheral.Characteristic(
        uuid=TX_UUID,
        properties=['notify'],
        value=b'',
        notify_callback=None
    )

    nus.add_characteristic(rx_char)
    nus.add_characteristic(tx_char)

    # Переферия
    periph = peripheral.Peripheral(
        adapter_addr=None,           # default адаптер (hci0)
        local_name=LOCAL_NAME,
        services=[nus]
    )

    # Коллбеки подписки
    def on_subscribe(_char, value, options):
        bridge.client_subscribed = True

    def on_unsubscribe(_char, value, options):
        bridge.client_subscribed = False

    # Bluezero не имеет отдельного hook, но можно обойтись флагом подписки.
    bridge.tx_char = tx_char

    # Главный цикл: крутим BLE и опрашиваем shell -> BLE
    try:
        periph.publish()  # запускает рекламу и GATT сервер (блокирующе)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.kill_shell()

if __name__ == '__main__':
    # Периодически вызываем tx_notifier в отдельном "тактировании" через select
    # Трюк: запускаем main() в дочернем процессе bluezero, а здесь — таймер.
    # Проще: monkey-patch peripheral.runloop с опросом. Но оставим просто отдельный таймер.
    # Практичнее — вынести цикл в поток, но bluezero держит ГЛ цикл dbus.
    # Поэтому используем сигнал-будильник для периодического tx_notifier.
    def alarm_handler(signum, frame):
        try:
            tx_notifier()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.02, 0.02)  # 50 Гц
    signal.signal(signal.SIGALRM, alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, 0.02, 0.02)
    main()
