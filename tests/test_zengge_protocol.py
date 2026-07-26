import unittest

from miot_x.integrations.zengge.protocol import (
    Session,
    build_pair_request,
    build_command_packet,
    build_dim_level_params,
    build_power_params,
    encrypt_command,
    generate_session_key,
    mesh_encrypt,
    parse_mac,
    parse_online_status_report,
    string_to_mesh_bytes,
    verify_pair_response,
)


class ZenggeProtocolTests(unittest.TestCase):
    def test_pair_request_matches_go_vector(self):
        request, random_a = build_pair_request(
            string_to_mesh_bytes("mesh-name-000001"),
            string_to_mesh_bytes("mesh-pass-000001"),
            random_a=bytes(range(1, 9)),
        )
        self.assertEqual(random_a, bytes(range(1, 9)))
        self.assertEqual(
            request.hex().upper(), "0C0102030405060708C1E3BC713A8A5146"
        )

    def test_pair_response_matches_go_vector(self):
        response = bytes.fromhex("0D1112131415161718C7A143D24CD70036")
        random_b = verify_pair_response(
            string_to_mesh_bytes("mesh-name-000001"),
            string_to_mesh_bytes("mesh-pass-000001"),
            response,
        )
        self.assertEqual(random_b, bytes(range(17, 25)))

    def test_pair_response_rejects_bad_proof(self):
        response = bytearray.fromhex("0D1112131415161718C7A143D24CD70036")
        response[-1] ^= 0xFF
        with self.assertRaisesRegex(ValueError, "authentication failed"):
            verify_pair_response(
                string_to_mesh_bytes("mesh-name-000001"),
                string_to_mesh_bytes("mesh-pass-000001"),
                bytes(response),
            )

    def test_mesh_encrypt_matches_go_vector(self):
        key = bytes(range(16))
        plain = bytes(range(16, 32))
        self.assertEqual(
            mesh_encrypt(key, plain).hex().upper(),
            "61B04D2D3166E91A0E6C16CAE8BBC0F6",
        )

    def test_session_key_matches_go_vector(self):
        key = generate_session_key(
            string_to_mesh_bytes("mesh-name-000001"),
            string_to_mesh_bytes("mesh-pass-000001"),
            bytes(range(1, 9)),
            bytes(range(17, 25)),
        )
        self.assertEqual(key.hex().upper(), "FA80F1DFE4E609C59545AFA6CBF7EE7D")

    def test_power_command_matches_go_vector(self):
        session_key = bytes.fromhex("FA80F1DFE4E609C59545AFA6CBF7EE7D")
        mac = parse_mac("AA:BB:CC:DD:EE:FF")
        packet = build_command_packet(
            0x123456, 1, 0xD0, build_power_params(0x0F, True)
        )
        self.assertEqual(
            packet.hex().upper(),
            "56341200000100D011020F01FF00000000030000",
        )
        self.assertEqual(
            encrypt_command(session_key, mac, 0x123456, packet).hex().upper(),
            "56341231CFF4D99BC31938599DD02EF3C9A4362A",
        )

    def test_brightness_command_matches_go_vector(self):
        session_key = bytes.fromhex("FA80F1DFE4E609C59545AFA6CBF7EE7D")
        mac = parse_mac("AA:BB:CC:DD:EE:FF")
        packet = build_command_packet(
            0x123457, 1, 0xE2, build_dim_level_params(0x0F, 30)
        )
        self.assertEqual(
            packet.hex().upper(),
            "57341200000100E211020F614D00000000020000",
        )
        self.assertEqual(
            encrypt_command(session_key, mac, 0x123457, packet).hex().upper(),
            "573412480758CBE31DA4179F02091724EB5ECCBF",
        )

    def test_session_sequence_wraps_and_never_uses_zero(self):
        session = Session(sequence=0xFFFFFE)
        self.assertEqual(session.next_sequence(), 0xFFFFFF)
        self.assertEqual(session.next_sequence(), 1)

    def test_online_status_parses_real_30_percent_capture(self):
        data = bytes.fromhex(
            "A0BBDE00005622DC110201911E40000000000000"
        )
        status = parse_online_status_report(data, mesh_address=1)
        self.assertTrue(status.online)
        self.assertTrue(status.is_on)
        self.assertEqual(status.brightness, 30)
        self.assertTrue(status.brightness_known)


if __name__ == "__main__":
    unittest.main()
