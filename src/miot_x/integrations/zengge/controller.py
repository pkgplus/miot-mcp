"""Async in-process controller for Zengge/Haodeng Telink BLE Mesh lights."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .protocol import (
    OP_CTRL_ON_OFF,
    OP_CTRL_SET_RGB,
    Session,
    build_command_packet,
    build_dim_level_params,
    build_pair_request,
    build_power_params,
    encrypt_command,
    generate_session_key,
    parse_mac,
    string_to_mesh_bytes,
    verify_pair_response,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
PAIR_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1914"
COMMAND_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
NOTIFY_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1911"

Scanner = Callable[[str, float], Awaitable[Any | None]]
ClientFactory = Callable[[Any, Callable[[Any], None]], Any]


async def _default_scanner(address: str, timeout: float) -> Any | None:
    from bleak import BleakScanner

    return await BleakScanner.find_device_by_address(address, timeout=timeout)


def _default_client_factory(device: Any, disconnected_callback: Callable[[Any], None]):
    from bleak import BleakClient

    return BleakClient(device, disconnected_callback=disconnected_callback)


class ZenggeController:
    """Keep a warm BLE session and serialize all mesh operations."""

    def __init__(
        self,
        *,
        mesh_name: str,
        mesh_password: str,
        mesh_ltk: str,
        device_mac: str,
        mesh_address: int = 1,
        control_type: int = 0x0F,
        idle_timeout: float = 60,
        scan_timeout: float = 30,
        settle_delay: float = 0.5,
        scanner: Scanner = _default_scanner,
        client_factory: ClientFactory = _default_client_factory,
    ) -> None:
        mesh_name_bytes = mesh_name.encode("utf-8")
        mesh_password_bytes = mesh_password.encode("utf-8")
        if not 1 <= len(mesh_name_bytes) <= 16:
            raise ValueError("mesh name must be 1-16 UTF-8 bytes")
        if not 1 <= len(mesh_password_bytes) <= 16:
            raise ValueError("mesh password must be 1-16 UTF-8 bytes")
        if len(mesh_ltk.encode("utf-8")) != 16:
            raise ValueError("mesh LTK must be exactly 16 UTF-8 bytes")
        if not 1 <= mesh_address <= 0xFFFF:
            raise ValueError("mesh address must be in 1..65535")
        if not 0 <= control_type <= 0xFF:
            raise ValueError("control type must be in 0..255")
        if idle_timeout < 0:
            raise ValueError("idle timeout must not be negative")
        if scan_timeout <= 0:
            raise ValueError("scan timeout must be positive")
        if settle_delay < 0:
            raise ValueError("settle delay must not be negative")
        self.mesh_name = string_to_mesh_bytes(mesh_name)
        self.mesh_password = string_to_mesh_bytes(mesh_password)
        self.mesh_ltk = mesh_ltk.encode("utf-8")
        self.device_mac = device_mac
        self._mac = parse_mac(device_mac)
        self.mesh_address = mesh_address
        self.control_type = control_type
        self.idle_timeout = idle_timeout
        self.scan_timeout = scan_timeout
        self.settle_delay = settle_delay
        self._scanner = scanner
        self._client_factory = client_factory
        self._client: Any | None = None
        self._session = Session()
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None

    @property
    def is_connected(self) -> bool:
        return bool(
            self._client is not None
            and self._client.is_connected
            and self._session.key is not None
        )

    async def execute(self, *, power: bool, brightness: int | None = None) -> None:
        async with self._lock:
            self._cancel_idle_disconnect()
            try:
                await self._ensure_connected()
                await self._send_command(
                    OP_CTRL_ON_OFF, build_power_params(self.control_type, power)
                )
                if brightness is not None:
                    await self._send_command(
                        OP_CTRL_SET_RGB,
                        build_dim_level_params(self.control_type, brightness),
                    )
                if self.settle_delay:
                    await asyncio.sleep(self.settle_delay)
            except BaseException:
                await asyncio.shield(self._disconnect_locked())
                raise
            else:
                self._schedule_idle_disconnect()

    async def refresh_device(self) -> bool:
        return (
            await self._scanner(self.device_mac, self.scan_timeout)
        ) is not None

    async def disconnect(self) -> None:
        async with self._lock:
            await self._disconnect_locked()

    async def stop(self) -> None:
        idle_task = self._idle_task
        self._idle_task = None
        if idle_task is not None:
            idle_task.cancel()
            try:
                await idle_task
            except asyncio.CancelledError:
                pass
        await self.disconnect()

    async def _ensure_connected(self) -> None:
        if self.is_connected:
            return
        await self._disconnect_locked()
        device = await self._scanner(self.device_mac, self.scan_timeout)
        if device is None:
            raise RuntimeError(f"BLE device not found: {self.device_mac}")
        client = self._client_factory(device, self._on_disconnected)
        self._client = client
        self._session = Session()
        try:
            await client.connect()
            await client.start_notify(NOTIFY_CHAR_UUID, self._handle_notification)
            await self._pair(client)
        except Exception:
            await self._disconnect_locked()
            raise

    async def _pair(self, client: Any) -> None:
        request, random_a = build_pair_request(self.mesh_name, self.mesh_password)
        await client.write_gatt_char(PAIR_CHAR_UUID, request, response=True)
        await client.write_gatt_char(NOTIFY_CHAR_UUID, b"\x01", response=True)
        response = bytes(await client.read_gatt_char(PAIR_CHAR_UUID))
        if response == b"\x0e":
            raise RuntimeError("mesh name or password rejected")
        random_b = verify_pair_response(self.mesh_name, self.mesh_password, response)
        self._session.key = generate_session_key(
            self.mesh_name, self.mesh_password, random_a, random_b
        )

    async def _send_command(self, opcode: int, params: bytes) -> None:
        if not self.is_connected:
            raise RuntimeError("BLE mesh session is not connected")
        sequence = self._session.next_sequence()
        packet = build_command_packet(
            sequence, self.mesh_address, opcode, params
        )
        encrypted = encrypt_command(
            self._session.key,
            self._mac,
            sequence,
            packet,
        )
        await self._client.write_gatt_char(
            COMMAND_CHAR_UUID, encrypted, response=False
        )

    def _handle_notification(self, sender: Any, data: bytearray) -> None:
        # Status decryption is added separately; control does not depend on it.
        return None

    def _on_disconnected(self, client: Any) -> None:
        if client is self._client:
            self._session.key = None

    async def _disconnect_locked(self) -> None:
        client = self._client
        self._client = None
        self._session.key = None
        if client is None:
            return
        if client.is_connected:
            try:
                await client.stop_notify(NOTIFY_CHAR_UUID)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception as exc:
                _LOGGER.warning("BlueZ 断开清理失败，已丢弃本地会话: %s", exc)

    def _cancel_idle_disconnect(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

    def _schedule_idle_disconnect(self) -> None:
        if self.idle_timeout <= 0:
            return
        self._idle_task = asyncio.create_task(
            self._disconnect_after_idle(), name="zengge-idle-disconnect"
        )

    async def _disconnect_after_idle(self) -> None:
        try:
            await asyncio.sleep(self.idle_timeout)
            async with self._lock:
                await self._disconnect_locked()
        except asyncio.CancelledError:
            pass
        finally:
            if self._idle_task is asyncio.current_task():
                self._idle_task = None
