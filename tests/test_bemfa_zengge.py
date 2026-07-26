import asyncio
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

    def test_redacts_mesh_credentials_from_cli_output(self):
        output = "Name=secret-name Pass=secret-pass LTK=secret-ltk connection failed"
        self.assertEqual(
            redact_secrets(output, "secret-name", "secret-pass", "secret-ltk"),
            "Name=*** Pass=*** LTK=*** connection failed",
        )

    def test_redacts_overlapping_credentials_longest_first(self):
        self.assertEqual(
            redact_secrets("abcdef abc", "abc", "abcdef", "abc"),
            "*** ***",
        )


class BluetoothRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_detects_bluez_connection_abort(self):
        self.assertTrue(is_recoverable_bluetooth_error("le-connection-abort-by-local"))
        self.assertTrue(is_recoverable_bluetooth_error("Device 00:11 not available"))
        self.assertFalse(is_recoverable_bluetooth_error("Adapter not available"))
        self.assertFalse(is_recoverable_bluetooth_error("org.bluez.Error.Failed"))
        self.assertFalse(is_recoverable_bluetooth_error("mesh password rejected"))

    async def test_recovers_and_retries_once(self):
        bridge = BemfaZenggeBridge(
            uid="uid",
            topic="topic002",
            cli_path="/fake/zengge-cli",
        )
        bridge._run_cli = AsyncMock(
            side_effect=[
                RuntimeError("le-connection-abort-by-local"),
                RuntimeError("le-connection-abort-by-local"),
                None,
                None,
            ]
        )
        bridge._restart_bluetooth = AsyncMock()
        bridge._refresh_bluez_device = AsyncMock()
        bridge._wait_before_retry = AsyncMock()

        await bridge.apply(BemfaCommand(power=True, brightness=30))

        bridge._restart_bluetooth.assert_awaited_once()
        bridge._refresh_bluez_device.assert_awaited_once()
        bridge._wait_before_retry.assert_awaited_once_with(2)
        self.assertEqual(bridge._run_cli.await_count, 4)

    async def test_does_not_recover_protocol_errors(self):
        bridge = BemfaZenggeBridge(
            uid="uid",
            topic="topic002",
            cli_path="/fake/zengge-cli",
        )
        bridge._run_cli = AsyncMock(side_effect=RuntimeError("mesh password rejected"))
        bridge._restart_bluetooth = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "mesh password rejected"):
            await bridge.apply(BemfaCommand(power=True))

        bridge._restart_bluetooth.assert_not_awaited()

    async def test_retries_fresh_scan_before_dropping_command(self):
        bridge = BemfaZenggeBridge(
            uid="uid", topic="topic002", cli_path="/fake/zengge-cli"
        )
        bridge._run_cli = AsyncMock(side_effect=[RuntimeError("Device 00:11 not available"), None])
        bridge._restart_bluetooth = AsyncMock()
        bridge._refresh_bluez_device = AsyncMock(
            side_effect=[RuntimeError("fresh BLE advertisement not found"), None]
        )
        bridge._wait_before_retry = AsyncMock()

        await bridge.apply(BemfaCommand(power=True))

        self.assertEqual(bridge._refresh_bluez_device.await_count, 2)
        bridge._wait_before_retry.assert_awaited_once_with(2)

    async def test_cli_credentials_are_passed_via_environment_not_argv(self):
        bridge = BemfaZenggeBridge(
            uid="uid",
            topic="topic002",
            cli_path="/fake/zengge-cli",
            mesh_name="mesh-name",
            mesh_pass="mesh-pass",
            mesh_ltk="1234567890abcdef",
        )
        bridge._run_process = AsyncMock(return_value=(0, "OK"))

        await bridge._run_cli("on")

        args = bridge._run_process.await_args.args[0]
        env = bridge._run_process.await_args.kwargs["env"]
        self.assertNotIn("mesh-pass", args)
        self.assertNotIn("1234567890abcdef", args)
        self.assertEqual(env["ZENGGE_MESH_PASS"], "mesh-pass")
        self.assertEqual(env["ZENGGE_MESH_LTK"], "1234567890abcdef")

    async def test_start_failure_rolls_back_mqtt_client(self):
        bridge = BemfaZenggeBridge(
            uid="uid", topic="topic002", cli_path=__file__
        )
        client = MagicMock()
        client.loop_start.side_effect = RuntimeError("network thread failed")
        with patch("miot_x.integrations.bemfa_zengge.mqtt.Client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "network thread failed"):
                await bridge.start()

        client.disconnect.assert_called_once()
        client.loop_stop.assert_called_once()
        self.assertIsNone(bridge._client)
        self.assertIsNone(bridge._loop)

    def test_mqtt_callback_has_bounded_pending_messages(self):
        bridge = BemfaZenggeBridge(
            uid="uid", topic="topic002", cli_path="/fake/zengge-cli"
        )
        callbacks = []
        bridge._loop = SimpleNamespace(call_soon_threadsafe=lambda callback: callbacks.append(callback))
        message = SimpleNamespace(payload=b"on")

        for _ in range(20):
            bridge._on_message(None, None, message)

        self.assertEqual(len(callbacks), 16)

    async def test_cancelled_process_is_terminated(self):
        bridge = BemfaZenggeBridge(
            uid="uid", topic="topic002", cli_path="/fake/zengge-cli"
        )
        task = asyncio.create_task(bridge._run_process(["/bin/sleep", "30"]))
        while bridge._process is None:
            await asyncio.sleep(0)
        process = bridge._process

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertIsNotNone(process.returncode)
        self.assertIsNone(bridge._process)


if __name__ == "__main__":
    unittest.main()
