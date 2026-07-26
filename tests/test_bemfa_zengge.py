import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from miot_x.integrations.bemfa_zengge import (
    BemfaCommand,
    BemfaZenggeBridge,
    is_recoverable_bluetooth_error,
    parse_bemfa_command,
    redact_secrets,
)


class ParseBemfaCommandTests(unittest.TestCase):
    def test_parses_power_and_brightness(self):
        self.assertEqual(parse_bemfa_command("on"), BemfaCommand(power=True))
        self.assertEqual(parse_bemfa_command("off"), BemfaCommand(power=False))
        self.assertEqual(
            parse_bemfa_command("on#30"),
            BemfaCommand(power=True, brightness=30),
        )

    def test_rejects_invalid_commands(self):
        for value in ("", "toggle", "on#0", "on#101", "on#30#x", "off#30"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_bemfa_command(value)

    def test_redacts_overlapping_credentials_longest_first(self):
        self.assertEqual(
            redact_secrets("abcdef abc", "abc", "abcdef", "abc"),
            "*** ***",
        )


class BemfaBridgeTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self, controller=None):
        controller = controller or SimpleNamespace(
            execute=AsyncMock(),
            disconnect=AsyncMock(),
            refresh_device=AsyncMock(return_value=True),
            stop=AsyncMock(),
        )
        bridge = BemfaZenggeBridge(
            uid="uid",
            topic="topic002",
            controller=controller,
        )
        return bridge, controller

    def test_detects_bluez_connection_abort(self):
        self.assertTrue(is_recoverable_bluetooth_error("le-connection-abort-by-local"))
        self.assertTrue(is_recoverable_bluetooth_error("Device 00:11 not available"))
        self.assertFalse(is_recoverable_bluetooth_error("Adapter not available"))
        self.assertFalse(is_recoverable_bluetooth_error("org.bluez.Error.Failed"))
        self.assertFalse(is_recoverable_bluetooth_error("BLE device not found"))
        self.assertFalse(is_recoverable_bluetooth_error("mesh password rejected"))

    async def test_power_and_brightness_are_one_controller_operation(self):
        bridge, controller = self.make_bridge()

        await bridge.apply(BemfaCommand(power=True, brightness=30))

        controller.execute.assert_awaited_once_with(power=True, brightness=30)

    async def test_recovers_bluez_and_retries_controller(self):
        controller = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[Exception("le-connection-abort-by-local"), None]
            ),
            disconnect=AsyncMock(),
            refresh_device=AsyncMock(return_value=True),
            stop=AsyncMock(),
        )
        bridge, _ = self.make_bridge(controller)
        bridge._restart_bluetooth = AsyncMock()
        bridge._wait_before_retry = AsyncMock()

        await bridge.apply(BemfaCommand(power=True))

        bridge._restart_bluetooth.assert_awaited_once()
        controller.disconnect.assert_awaited_once()
        controller.refresh_device.assert_awaited_once()
        self.assertEqual(controller.execute.await_count, 2)

    async def test_retries_fresh_scan_before_dropping_command(self):
        controller = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[RuntimeError("Device 00:11 not available"), None]
            ),
            disconnect=AsyncMock(),
            refresh_device=AsyncMock(side_effect=[False, True]),
            stop=AsyncMock(),
        )
        bridge, _ = self.make_bridge(controller)
        bridge._restart_bluetooth = AsyncMock()
        bridge._wait_before_retry = AsyncMock()

        await bridge.apply(BemfaCommand(power=False))

        self.assertEqual(controller.refresh_device.await_count, 2)
        bridge._wait_before_retry.assert_awaited_once_with(2)

    async def test_retries_when_fresh_scan_raises_transient_error(self):
        controller = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[Exception("le-connection-abort-by-local"), None]
            ),
            disconnect=AsyncMock(),
            refresh_device=AsyncMock(
                side_effect=[Exception("DBus disconnected"), True]
            ),
            stop=AsyncMock(),
        )
        bridge, _ = self.make_bridge(controller)
        bridge._restart_bluetooth = AsyncMock()
        bridge._wait_before_retry = AsyncMock()

        await bridge.apply(BemfaCommand(power=True))

        self.assertEqual(controller.refresh_device.await_count, 2)
        bridge._wait_before_retry.assert_awaited_once_with(2)

    async def test_does_not_recover_protocol_errors(self):
        controller = SimpleNamespace(
            execute=AsyncMock(side_effect=RuntimeError("mesh password rejected")),
            disconnect=AsyncMock(),
            refresh_device=AsyncMock(),
            stop=AsyncMock(),
        )
        bridge, _ = self.make_bridge(controller)
        bridge._restart_bluetooth = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "mesh password rejected"):
            await bridge.apply(BemfaCommand(power=True))

        bridge._restart_bluetooth.assert_not_awaited()

    async def test_stop_closes_mqtt_and_ble_controller(self):
        bridge, controller = self.make_bridge()
        client = MagicMock()
        bridge._client = client

        await bridge.stop()

        client.disconnect.assert_called_once()
        client.loop_stop.assert_called_once()
        controller.stop.assert_awaited_once()

    async def test_process_timeout_terminates_child(self):
        bridge, _ = self.make_bridge()

        with self.assertRaisesRegex(RuntimeError, "timed out"):
            await bridge._run_process(["/bin/sleep", "30"], timeout=0.01)

        self.assertIsNone(bridge._process)

    async def test_start_failure_rolls_back_mqtt_client(self):
        bridge, controller = self.make_bridge()
        client = MagicMock()
        client.loop_start.side_effect = RuntimeError("network thread failed")
        with patch("miot_x.integrations.bemfa_zengge.mqtt.Client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "network thread failed"):
                await bridge.start()

        client.disconnect.assert_called_once()
        client.loop_stop.assert_called_once()
        controller.stop.assert_awaited_once()
        self.assertIsNone(bridge._client)
        self.assertIsNone(bridge._loop)

    def test_mqtt_callback_has_bounded_pending_messages(self):
        bridge, _ = self.make_bridge()
        callbacks = []
        bridge._loop = SimpleNamespace(
            call_soon_threadsafe=lambda callback: callbacks.append(callback)
        )
        message = SimpleNamespace(payload=b"on")

        for _ in range(20):
            bridge._on_message(None, None, message)

        self.assertEqual(len(callbacks), 16)

    def test_from_env_requires_device_mac(self):
        env = {
            "BEMFA_UID": "uid",
            "ZENGGE_MESH_NAME": "mesh-name-000001",
            "ZENGGE_MESH_PASS": "mesh-pass-000001",
            "ZENGGE_MESH_LTK": "ltk-key-00000001",
            "ZENGGE_DEVICE_MAC": "",
        }
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(ValueError, "ZENGGE_DEVICE_MAC"):
                BemfaZenggeBridge.from_env()


if __name__ == "__main__":
    unittest.main()
