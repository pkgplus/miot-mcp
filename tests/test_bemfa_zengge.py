import unittest
from unittest.mock import AsyncMock

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
        output = "MeshConfig{Pass:secret-pass} LTK=secret-ltk connection failed"
        self.assertEqual(
            redact_secrets(output, "secret-pass", "secret-ltk"),
            "MeshConfig{Pass:***} LTK=*** connection failed",
        )


class BluetoothRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_detects_bluez_connection_abort(self):
        self.assertTrue(is_recoverable_bluetooth_error("le-connection-abort-by-local"))
        self.assertTrue(is_recoverable_bluetooth_error("Device 00:11 not available"))
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


if __name__ == "__main__":
    unittest.main()
