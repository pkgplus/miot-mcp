import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from miot_x.integrations.zengge.controller import (
    COMMAND_CHAR_UUID,
    NOTIFY_CHAR_UUID,
    PAIR_CHAR_UUID,
    ZenggeController,
)


class FakeBleakClient:
    def __init__(self, device, disconnected_callback=None):
        self.device = device
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.connect = AsyncMock(side_effect=self._connect)
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.start_notify = AsyncMock()
        self.stop_notify = AsyncMock()
        self.write_gatt_char = AsyncMock()
        self.read_gatt_char = AsyncMock(
            return_value=bytes.fromhex("0D1112131415161718C7A143D24CD70036")
        )

    async def _connect(self):
        self.is_connected = True

    async def _disconnect(self):
        self.is_connected = False


class ZenggeControllerTests(unittest.IsolatedAsyncioTestCase):
    def test_rejects_invalid_configuration_before_ble_io(self):
        common = {
            "mesh_name": "mesh-name-000001",
            "mesh_password": "mesh-pass-000001",
            "mesh_ltk": "ltk-key-00000001",
            "device_mac": "AA:BB:CC:DD:EE:FF",
        }
        invalid = [
            ({"mesh_name": "x" * 17}, "mesh name"),
            ({"mesh_password": ""}, "mesh password"),
            ({"mesh_address": 0x10000}, "mesh address"),
            ({"control_type": 0x100}, "control type"),
            ({"idle_timeout": -1}, "idle timeout"),
            ({"device_mac": "invalid"}, "MAC"),
        ]
        for overrides, message in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, message):
                    ZenggeController(**(common | overrides))

    def make_controller(self, idle_timeout: float = 60) -> ZenggeController:
        self.device = object()
        self.scanner = AsyncMock(return_value=self.device)
        self.clients = []

        def client_factory(device, disconnected_callback):
            client = FakeBleakClient(device, disconnected_callback)
            self.clients.append(client)
            return client

        return ZenggeController(
            mesh_name="mesh-name-000001",
            mesh_password="mesh-pass-000001",
            mesh_ltk="ltk-key-00000001",
            device_mac="AA:BB:CC:DD:EE:FF",
            mesh_address=1,
            control_type=0x0F,
            idle_timeout=idle_timeout,
            scanner=self.scanner,
            client_factory=client_factory,
            settle_delay=0,
        )

    async def test_power_and_brightness_share_one_ble_connection(self):
        controller = self.make_controller()

        await controller.execute(power=True, brightness=30)

        self.scanner.assert_awaited_once()
        client = self.clients[0]
        client.connect.assert_awaited_once()
        command_writes = [
            call
            for call in client.write_gatt_char.await_args_list
            if call.args[0] == COMMAND_CHAR_UUID
        ]
        self.assertEqual(len(command_writes), 2)
        self.assertTrue(all(call.kwargs["response"] is False for call in command_writes))
        self.assertTrue(client.is_connected)
        await controller.stop()

    async def test_second_command_reuses_hot_connection(self):
        controller = self.make_controller()

        await controller.execute(power=True)
        await controller.execute(power=False)

        self.scanner.assert_awaited_once()
        self.clients[0].connect.assert_awaited_once()
        await controller.stop()

    async def test_pairing_uses_expected_characteristics(self):
        controller = self.make_controller()

        await controller.execute(power=True)

        client = self.clients[0]
        client.start_notify.assert_awaited_once()
        pair_write = client.write_gatt_char.await_args_list[0]
        self.assertEqual(pair_write.args[0], PAIR_CHAR_UUID)
        self.assertTrue(pair_write.kwargs["response"])
        notify_enable = client.write_gatt_char.await_args_list[1]
        self.assertEqual(notify_enable.args, (NOTIFY_CHAR_UUID, b"\x01"))
        self.assertTrue(notify_enable.kwargs["response"])
        client.read_gatt_char.assert_awaited_once_with(PAIR_CHAR_UUID)
        await controller.stop()

    async def test_idle_timeout_disconnects_hot_connection(self):
        controller = self.make_controller(idle_timeout=0.01)

        await controller.execute(power=True)
        await asyncio.sleep(0.03)

        self.clients[0].disconnect.assert_awaited_once()
        self.assertFalse(controller.is_connected)
        await controller.stop()

    async def test_scan_failure_is_reported(self):
        controller = self.make_controller()
        self.scanner.return_value = None

        with self.assertRaisesRegex(RuntimeError, "not found"):
            await controller.execute(power=True)

        self.assertEqual(self.clients, [])
        await controller.stop()

    async def test_disconnect_callback_invalidates_session(self):
        controller = self.make_controller()
        await controller.execute(power=True)
        client = self.clients[0]

        client.is_connected = False
        client.disconnected_callback(client)
        await asyncio.sleep(0)

        self.assertFalse(controller.is_connected)
        await controller.stop()

    async def test_reconnect_creates_a_fresh_session(self):
        controller = self.make_controller()
        await controller.execute(power=True)
        first_session = controller._session
        client = self.clients[0]
        client.is_connected = False
        client.disconnected_callback(client)

        await controller.execute(power=False)

        self.assertIsNot(controller._session, first_session)
        self.assertEqual(len(self.clients), 2)
        await controller.stop()

    async def test_stop_tolerates_bluez_disconnect_error(self):
        controller = self.make_controller()
        await controller.execute(power=True)
        client = self.clients[0]
        client.disconnect.side_effect = Exception("BlueZ object already gone")

        await controller.stop()

        self.assertFalse(controller.is_connected)

    async def test_write_failure_disconnects_instead_of_leaking_hot_connection(self):
        controller = self.make_controller(idle_timeout=0.01)
        try:
            await controller.execute(power=True)
            client = self.clients[0]
            client.write_gatt_char.side_effect = Exception("write failed")

            with self.assertRaisesRegex(Exception, "write failed"):
                await controller.execute(power=False)

            await asyncio.sleep(0.02)
            self.assertFalse(controller.is_connected)
            client.disconnect.assert_awaited_once()
        finally:
            await controller.stop()

    async def test_cancelled_command_disconnects_hot_connection(self):
        controller = self.make_controller()
        await controller.execute(power=True)
        client = self.clients[0]
        blocked = asyncio.Event()

        async def wait_forever(*args, **kwargs):
            blocked.set()
            await asyncio.Future()

        client.write_gatt_char.side_effect = wait_forever
        task = asyncio.create_task(controller.execute(power=False))
        await blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertFalse(controller.is_connected)
        client.disconnect.assert_awaited_once()
        await controller.stop()


if __name__ == "__main__":
    unittest.main()
