# -*- coding: utf-8 -*-
"""Bemfa MQTT to Zengge BLE Mesh bridge managed by miot-x."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BemfaCommand:
    power: bool
    brightness: int | None = None


def parse_bemfa_command(payload: str) -> BemfaCommand:
    parts = payload.strip().split("#")
    if parts[0] == "off" and len(parts) == 1:
        return BemfaCommand(power=False)
    if parts[0] != "on" or len(parts) > 2:
        raise ValueError(f"unsupported command: {payload!r}")
    if len(parts) == 1:
        return BemfaCommand(power=True)
    try:
        brightness = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"invalid brightness: {parts[1]!r}") from exc
    if not 1 <= brightness <= 100:
        raise ValueError(f"brightness out of range: {brightness}")
    return BemfaCommand(power=True, brightness=brightness)


def is_recoverable_bluetooth_error(message: str) -> bool:
    text = message.lower()
    return any(
        marker in text
        for marker in (
            "le-connection-abort-by-local",
            "not available",
            "org.bluez.error.failed",
            "software caused connection abort",
        )
    )


def redact_secrets(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


class BemfaZenggeBridge:
    """Receive Bemfa commands and execute them through zengge-cli."""

    def __init__(
        self,
        *,
        uid: str,
        topic: str,
        cli_path: str,
        broker: str = "bemfa.com",
        port: int = 9501,
        mesh_name: str = "",
        mesh_pass: str = "",
        mesh_ltk: str = "",
        device_mac: str = "08:65:F0:79:A3:C2",
    ) -> None:
        self.uid = uid
        self.topic = topic
        self.cli_path = cli_path
        self.broker = broker
        self.port = port
        self.mesh_name = mesh_name
        self.mesh_pass = mesh_pass
        self.mesh_ltk = mesh_ltk
        self.device_mac = device_mac
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=16)
        self._worker_task: asyncio.Task | None = None
        self._client: mqtt.Client | None = None

    @classmethod
    def from_env(cls) -> BemfaZenggeBridge | None:
        uid = os.getenv("BEMFA_UID", "").strip()
        if not uid:
            return None
        return cls(
            uid=uid,
            topic=os.getenv("BEMFA_TOPIC", "NG7LCB84e002").strip(),
            cli_path=os.getenv(
                "ZENGGE_CLI_PATH", "/home/pi5/src/zengge-sdk/zengge-cli"
            ).strip(),
            broker=os.getenv("BEMFA_HOST", "bemfa.com").strip(),
            port=int(os.getenv("BEMFA_PORT", "9501")),
            mesh_name=os.getenv("ZENGGE_MESH_NAME", "").strip(),
            mesh_pass=os.getenv("ZENGGE_MESH_PASS", "").strip(),
            mesh_ltk=os.getenv("ZENGGE_MESH_LTK", "").strip(),
            device_mac=os.getenv("ZENGGE_DEVICE_MAC", "08:65:F0:79:A3:C2").strip(),
        )

    async def start(self) -> None:
        if not Path(self.cli_path).is_file():
            raise FileNotFoundError(f"zengge-cli not found: {self.cli_path}")
        self._loop = asyncio.get_running_loop()
        self._worker_task = asyncio.create_task(
            self._worker(), name="bemfa-zengge-worker"
        )
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.uid,
            protocol=mqtt.MQTTv311,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=2, max_delay=30)
        client.connect_async(self.broker, self.port, keepalive=30)
        client.loop_start()
        self._client = client
        _LOGGER.info("巴法鱼缸灯桥接已启动: %s:%d/%s", self.broker, self.port, self.topic)

    async def stop(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()
            self._client = None
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0:
            _LOGGER.error("巴法 MQTT 连接失败: %s", reason_code)
            return
        client.subscribe(self.topic, qos=1)
        _LOGGER.info("巴法 MQTT 已订阅: %s", self.topic)

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties
    ) -> None:
        if reason_code != 0:
            _LOGGER.warning("巴法 MQTT 连接中断，将自动重连: %s", reason_code)

    def _on_message(self, client, userdata, message) -> None:
        if self._loop is None:
            return
        payload = message.payload.decode("utf-8", errors="replace")

        def enqueue() -> None:
            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                _LOGGER.error("巴法命令队列已满，丢弃: %r", payload)

        self._loop.call_soon_threadsafe(enqueue)

    async def _worker(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                command = parse_bemfa_command(payload)
                await self.apply(command)
                self._publish_state(payload.strip())
                _LOGGER.info("鱼缸灯命令已执行: %s", payload.strip())
            except Exception as exc:
                _LOGGER.error("鱼缸灯命令执行失败 %r: %s", payload, exc)
            finally:
                self._queue.task_done()

    async def apply(self, command: BemfaCommand) -> None:
        operations: list[tuple[str, int | None]] = [
            ("on" if command.power else "off", None)
        ]
        if command.brightness is not None:
            operations.append(("bright", command.brightness))

        index = 0
        while index < len(operations):
            name, brightness = operations[index]
            recovery_attempts = 0
            bluetooth_restarted = False
            while True:
                try:
                    await self._run_cli(name, brightness)
                    index += 1
                    break
                except RuntimeError as exc:
                    if not is_recoverable_bluetooth_error(str(exc)):
                        raise
                    if recovery_attempts >= 3:
                        raise
                    recovery_attempts += 1
                    if not bluetooth_restarted:
                        bluetooth_restarted = True
                        _LOGGER.warning(
                            "检测到 BlueZ 连接中断，重置蓝牙后重试: %s", exc
                        )
                        await self._restart_bluetooth()
                        await self._refresh_bluez_device()
                    else:
                        delay = 2 ** (recovery_attempts - 1)
                        _LOGGER.warning(
                            "BlueZ 恢复后连接仍中断，%d 秒后重试 (%d/3)",
                            delay,
                            recovery_attempts,
                        )
                        await self._wait_before_retry(delay)

    async def _wait_before_retry(self, delay: int) -> None:
        await asyncio.sleep(delay)

    async def _run_cli(self, command: str, brightness: int | None = None) -> None:
        args = [self.cli_path, "-scan", "-timeout", "30s", "-cmd", command]
        if self.mesh_name:
            args.extend(["-mesh-name", self.mesh_name])
        if self.mesh_pass:
            args.extend(["-mesh-pass", self.mesh_pass])
        if self.mesh_ltk:
            args.extend(["-mesh-ltk", self.mesh_ltk])
        if self.device_mac:
            args.extend(["-mac", self.device_mac])
        if brightness is not None:
            args.extend(["-bright", str(brightness)])

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        text = redact_secrets(
            output.decode("utf-8", errors="replace").strip(),
            self.mesh_pass,
            self.mesh_ltk,
        )
        if process.returncode != 0:
            raise RuntimeError(text or f"zengge-cli exited {process.returncode}")

    async def _restart_bluetooth(self) -> None:
        process = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            "systemctl",
            "restart",
            "bluetooth.service",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                "restart bluetooth failed: "
                + output.decode("utf-8", errors="replace").strip()
            )
        await asyncio.sleep(2)

    async def _refresh_bluez_device(self) -> None:
        """Hold an active discovery session until BlueZ sees a fresh advertisement."""
        process = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            "--timeout",
            "10",
            "scan",
            "on",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        text = output.decode("utf-8", errors="replace")
        if process.returncode != 0 or self.device_mac.lower() not in text.lower():
            raise RuntimeError(
                f"fresh BLE advertisement not found for {self.device_mac}"
            )

    def _publish_state(self, payload: str) -> None:
        if self._client is not None:
            self._client.publish(self.topic + "/up", payload, qos=0, retain=False)
