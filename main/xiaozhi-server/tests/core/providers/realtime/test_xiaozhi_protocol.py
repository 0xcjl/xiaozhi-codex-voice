import unittest

from core.providers.realtime.xiaozhi_protocol import AudioPacket, pack_audio, parse_hello, unpack_audio


class ProtocolTests(unittest.TestCase):
    def test_hello_negotiates_exact_version_and_format(self):
        hello = {"type": "hello", "version": 2, "transport": "websocket", "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
        self.assertEqual(parse_hello(hello), (2, 16000, 60))
        hello["audio_params"]["channels"] = 2
        with self.assertRaises(ValueError):
            parse_hello(hello)

    def test_versioned_frames_are_big_endian_and_checked(self):
        packet = AudioPacket(b"\xaa\xbb", 258)
        self.assertEqual(pack_audio(1, packet), b"\xaa\xbb")
        self.assertEqual(pack_audio(2, packet), bytes.fromhex("00020000000000000000010200000002aabb"))
        self.assertEqual(pack_audio(3, packet), bytes.fromhex("00000002aabb"))
        self.assertEqual(unpack_audio(2, pack_audio(2, packet)), packet)
        self.assertEqual(unpack_audio(3, pack_audio(3, packet)), AudioPacket(packet.opus))
        for version, malformed in ((2, bytes.fromhex("00020000000000000000010200000003aabb")), (3, bytes.fromhex("00000003aabb"))):
            with self.assertRaises(ValueError):
                unpack_audio(version, malformed)


if __name__ == "__main__":
    unittest.main()
