"""Telink BLE Mesh packet and crypto primitives."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

VENDOR_ID = 0x1102
OP_CTRL_ON_OFF = 0xD0
OP_CTRL_ONLINE_STATUS = 0xDC
OP_CTRL_SET_RGB = 0xE2


@dataclass(frozen=True)
class LightStatus:
    online: bool
    is_on: bool
    brightness: int = 0
    brightness_known: bool = False


class Session:
    def __init__(self, sequence: int | None = None) -> None:
        if sequence is None:
            sequence = secrets.randbelow(0xFFFFFE) + 1
        self.sequence = sequence
        self.key: bytes | None = None

    def next_sequence(self) -> int:
        self.sequence += 1
        if self.sequence > 0xFFFFFF:
            self.sequence = 1
        return self.sequence


def string_to_mesh_bytes(value: str) -> bytes:
    return value.encode("utf-8")[:16].ljust(16, b"\x00")


def mesh_encrypt(key: bytes, plaintext: bytes) -> bytes:
    if len(key) != 16 or len(plaintext) != 16:
        raise ValueError("key and plaintext must be 16 bytes")
    cipher = Cipher(algorithms.AES(key[::-1]), modes.ECB())
    encryptor = cipher.encryptor()
    return (encryptor.update(plaintext[::-1]) + encryptor.finalize())[::-1]


def generate_session_key(
    mesh_name: bytes, mesh_pass: bytes, random_a: bytes, random_b: bytes
) -> bytes:
    if len(mesh_name) != 16 or len(mesh_pass) != 16:
        raise ValueError("mesh name and password must be 16 bytes")
    if len(random_a) != 8 or len(random_b) != 8:
        raise ValueError("pairing random values must be 8 bytes")
    name_pass = bytes(a ^ b for a, b in zip(mesh_name, mesh_pass))
    return mesh_encrypt(name_pass, random_a + random_b)


def build_pair_request(
    mesh_name: bytes, mesh_pass: bytes, random_a: bytes | None = None
) -> tuple[bytes, bytes]:
    if len(mesh_name) != 16 or len(mesh_pass) != 16:
        raise ValueError("mesh name and password must be 16 bytes")
    random_a = random_a or secrets.token_bytes(8)
    if len(random_a) != 8:
        raise ValueError("pairing random value must be 8 bytes")
    name_pass = bytes(a ^ b for a, b in zip(mesh_name, mesh_pass))
    key = random_a + bytes(8)
    proof = mesh_encrypt(key, name_pass)
    return bytes((0x0C,)) + random_a + proof[:8], random_a


def verify_pair_response(
    mesh_name: bytes, mesh_pass: bytes, response: bytes
) -> bytes:
    if len(response) < 17 or response[0] != 0x0D:
        raise ValueError("invalid pair response")
    name_pass = bytes(a ^ b for a, b in zip(mesh_name, mesh_pass))
    random_b = response[1:9]
    proof = mesh_encrypt(random_b + bytes(8), name_pass)
    if not secrets.compare_digest(response[9:17], proof[:8]):
        raise ValueError("pair response authentication failed")
    return random_b


def parse_mac(mac: str) -> bytes:
    raw = mac.replace(":", "").replace("-", "")
    try:
        value = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError(f"invalid MAC address: {mac!r}") from exc
    if len(value) != 6:
        raise ValueError(f"invalid MAC address: {mac!r}")
    return value[::-1]


def build_command_packet(
    sequence: int, destination: int, opcode: int, params: bytes
) -> bytes:
    packet = bytearray(20)
    packet[0:3] = sequence.to_bytes(3, "little")
    packet[5:7] = destination.to_bytes(2, "little")
    packet[7] = opcode | 0xC0
    packet[8:10] = VENDOR_ID.to_bytes(2, "big")
    packet[10 : 10 + min(len(params), 10)] = params[:10]
    return bytes(packet)


def build_power_params(control_type: int, on: bool) -> bytes:
    return bytes((control_type, 0x01, 0xFF if on else 0x00, 0, 0, 0, 0, 0x03, 0))


def build_dim_level_params(control_type: int, brightness: int) -> bytes:
    brightness = max(0, min(100, brightness))
    level = (brightness * 255 + 50) // 100
    return bytes((control_type, 0x61, level, 0, 0, 0, 0, 0x02, 0))


def _checksum(key: bytes, nonce: bytes, payload: bytes) -> bytes:
    block = bytearray(16)
    block[: len(nonce)] = nonce
    block[len(nonce)] = len(payload)
    check = mesh_encrypt(key, bytes(block))
    for offset in range(0, len(payload), 16):
        chunk = payload[offset : offset + 16]
        mixed = bytearray(check)
        for index, value in enumerate(chunk):
            mixed[index] ^= value
        check = mesh_encrypt(key, bytes(mixed))
    return check


def _crypt_payload(key: bytes, nonce: bytes, payload: bytes) -> bytes:
    counter = bytearray(16)
    counter[1 : 1 + len(nonce)] = nonce
    result = bytearray(len(payload))
    for offset in range(0, len(payload), 16):
        stream = mesh_encrypt(key, bytes(counter))
        chunk = payload[offset : offset + 16]
        for index, value in enumerate(chunk):
            result[offset + index] = value ^ stream[index]
        counter[0] = (counter[0] + 1) & 0xFF
    return bytes(result)


def encrypt_command(
    session_key: bytes, mac_address: bytes, sequence: int, packet: bytes
) -> bytes:
    if len(session_key) != 16 or len(mac_address) != 6 or len(packet) != 20:
        raise ValueError("invalid key, MAC, or command length")
    nonce = mac_address[:4] + bytes((1,)) + sequence.to_bytes(3, "little")
    payload = packet[5:]
    check = _checksum(session_key, nonce, payload)
    encrypted = _crypt_payload(session_key, nonce, payload)
    return packet[:3] + check[:2] + encrypted


def parse_online_status_report(data: bytes, mesh_address: int) -> LightStatus:
    if len(data) < 20:
        raise ValueError("data too short")
    if data[7] != (OP_CTRL_ONLINE_STATUS | 0xC0):
        raise ValueError(f"unexpected opcode: 0x{data[7]:02X}")
    for offset in range(10, 20, 5):
        record = data[offset : offset + 5]
        if record[0] == mesh_address:
            brightness = record[2]
            return LightStatus(
                online=record[1] != 0,
                is_on=brightness > 0,
                brightness=brightness,
                brightness_known=True,
            )
    raise ValueError("device not present in online status report")
