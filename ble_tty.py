#!/usr/bin/env python3
import os, pty, fcntl, termios, select, signal, asyncio
from dbus_next.aio import MessageBus
from dbus_next.service import (ServiceInterface, method, dbus_property, signal as dbus_signal, PropertyAccess)
from dbus_next import Variant

# BlueZ D-Bus constants
BLUEZ_SERVICE = 'org.bluez'
ADAPTER_IFACE = 'org.bluez.Adapter1'
LE_ADV_IFACE  = 'org.bluez.LEAdvertisingManager1'
GATT_MGR_IFACE= 'org.bluez.GattManager1'
GATT_SERVICE_IFACE = 'org.bluez.GattService1'
GATT_CHRC_IFACE    = 'org.bluez.GattCharacteristic1'

# Nordic UART Service UUIDs
UART_UUID = '6e400001-b5a3-f393-e0a9-e50e24dcca9e'
TX_UUID   = '6e400003-b5a3-f393-e0a9-e50e24dcca9e'  # notify: RPi -> phone
RX_UUID   = '6e400002-b5a3-f393-e0a9-e50e24dcca9e'  # write:  phone -> RPi
LOCAL_NAME = 'RPi-BLE-UART'
MAX_CHUNK = 180

########################################################################
# GATT application tree (Service + Characteristics), чисто dbus-next
########################################################################

class Application(ServiceInterface):
    def __init__(self, bus, adapter_path, service, tx_chrc, rx_chrc):
        super().__init__('org.bluez.GattApplication1')  # неофициальный интерфейс, нужен для экспортирования дерева
        self.bus = bus
        self.adapter_path = adapter_path
        self.service = service
        self.tx_chrc = tx_chrc
        self.rx_chrc = rx_chrc

class GattService(ServiceInterface):
    def __init__(self, uuid, primary=True):
        super().__init__(GATT_SERVICE_IFACE)
        self.uuid = uuid
        self.primary = primary
        self.includes = []

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> 's':
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> 'b':
        return self.primary

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> 'ao':
        return []

class GattCharacteristic(ServiceInterface):
    def __init__(self, uuid, flags):
        super().__init__(GATT_CHRC_IFACE)
        self.uuid = uuid
        self.flags = flags
        self._notifying = False
        self._svc_path = None
        self._value = bytearray()

    def set_service_path(self, path):
        self._svc_path = path

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> 's':
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> 'o':
        return self._svc_path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> 'as':
        return self.flags

    # BlueZ вызывает ReadValue/WriteValue/StartNotify/StopNotify
    @method()
    def ReadValue(self, options: 'a{sv}') -> 'ay':
        return bytes(self._value)

    @method()
    def WriteValue(self, value: 'ay', options: 'a{sv}'):
        # override in subclass
        self._value = bytearray(value)

    @method()
    def StartNotify(self):
        self._notifying = True

    @method()
    def StopNotify(self):
        self._notifying = False

    def is_notifying(self):
        return self._notifying

########################################################################
# Наши конкретные характеристики для NUS
########################################################################

class RxCharacteristic(GattCharacteristic):
    def __init__(self, on_rx):
        super().__init__(RX_UUID, ['write', 'write-without-response'])
        self.on_rx = on_rx

    @method()
    def WriteValue(self, value: 'ay', options: 'a{sv}'):
        # из телефона -> в шелл
        if value:
            self.on_rx(bytes(value))

class TxCharacteristic(GattCharacteristic):
    def __init__(self):
        super().__init__(TX_UUID, ['notify'])
        self.notify_subs = []

########################################################################
# Реклама (LEAdvertising1) — используем simplifed adapter SetAlias
########################################################################

async def get_adapter_path(bus):
    obj = await bus.introspect(BLUEZ_SERVICE, '/')
    m = bus.get_proxy_object(BLUEZ_SERVICE, '/', obj)
    objs = await m.introspect()
    for node in objs.nodes:
        path = '/' + node.name
        try:
            int_obj = await bus.introspect(BLUEZ_SERVICE, path)
            adapter = bus.get_proxy_object(BLUEZ_SERVICE, path, int_obj).get_interface(ADAPTER_IFACE)
            return path
        except Exception:
            continue
    raise RuntimeError('BLE adapter not found')

########################################################################
# PTY шелл
########################################################################

class ShellBridge:
    def __init__(self):
        self.master_fd = None
        self.child_pid = None

    def spawn_shell(self):
        master, slave = pty.openpty()
        for fd in (master, slave):
            attrs = termios.tcgetattr(fd)
            attrs[3] = attrs[3] | termios.ECHO | termios.ICANON
            termios.tcsetattr(fd, termios.TCSANOW, attrs)

        pid = os.fork()
        if pid == 0:
            os.setsid()
            os.close(master)
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            if slave > 2:
                os.close(slave)
            os.execvp('/bin/bash', ['/bin/bash', '-l'])
        else:
            os.close(slave)
            fl = fcntl.fcntl(master, fcntl.F_GETFL)
            fcntl.fcntl(master, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            self.master_fd = master
            self.child_pid = pid

    def write(self, data: bytes):
        if self.master_fd is not None:
            os.write(self.master_fd, data)

    def read(self) -> bytes:
        if self.master_fd is None:
            return b''
        r, _, _ = select.select([self.master_fd], [], [], 0)
        if self.master_fd in r:
            try:
                return os.read(self.master_fd, 4096)
            except OSError:
                return b''
        return b''

########################################################################
# Main
########################################################################

async def main():
    bus = await MessageBus().connect()

    adapter_path = await get_adapter_path(bus)

    # Переименуем устройство (не обязательно)
    try:
        adp_obj = await bus.introspect(BLUEZ_SERVICE, adapter_path)
        adapter = bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, adp_obj).get_interface(ADAPTER_IFACE)
        await adapter.call_set_alias(LOCAL_NAME)
        await adapter.call_set_powered(True)
        await adapter.call_set_discoverable(True)
    except Exception:
        pass

    # Сервис и характеристики
    service = GattService(UART_UUID, primary=True)
    shell = ShellBridge()
    shell.spawn_shell()

    tx = TxCharacteristic()
    rx = RxCharacteristic(on_rx=shell.write)

    # Экспортируем объекты на шине
    app_path = '/com/example/bleuart'
    svc_path = app_path + '/service0'
    tx_path  = svc_path + '/char0'
    rx_path  = svc_path + '/char1'

    service.set_service_path = lambda p: None
    tx.set_service_path(svc_path)
    rx.set_service_path(svc_path)

    bus.export(svc_path, service)
    bus.export(tx_path, tx)
    bus.export(rx_path, rx)

    # Регистрируем приложение в BlueZ
    gatt_mgr_obj = await bus.introspect(BLUEZ_SERVICE, adapter_path)
    gatt_mgr = bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, gatt_mgr_obj).get_interface(GATT_MGR_IFACE)

    # Дерево приложения — список корневых путей сервисов
    # BlueZ ожидает объект GattApplication1 (у нас нет явного интерфейса, BlueZ примет просто список)
    # В dbus-next регистрируем через RegisterApplication на путь корня; BlueZ сам обойдёт дочерние.
    try:
        await gatt_mgr.call_register_application(app_path, {})
    except Exception as e:
        # Если упало — значит BlueZ не увидел дерево; экспортируем фиктивный интерфейс на app_path
        class Dummy(ServiceInterface):
            def __init__(self): super().__init__('org.bluez.GattApplication1')
        bus.export(app_path, Dummy())
        await gatt_mgr.call_register_application(app_path, {})

    # Простой цикл опроса PTY и отправки notify
    async def tx_pump():
        while True:
            data = shell.read()
            if data:
                start = 0
                ln = len(data)
                while start < ln:
                    chunk = data[start:start+MAX_CHUNK]
                    # Обновляем значение; BlueZ разошлёт нотификацию сам, если клиент подписан
                    tx._value = bytearray(chunk)
                    # Вызов PropertiesChanged, чтобы BlueZ увидел новое значение (не обязателен, но помогает)
                    bus.emit_properties_changed(tx, GATT_CHRC_IFACE, {'Value': Variant('ay', tx._value)}, [])
                    start += MAX_CHUNK
            await asyncio.sleep(0.02)

    await tx_pump()

if __name__ == '__main__':
    signal.signal(signal.SIGINT, lambda *a: os._exit(0))
    asyncio.run(main())
