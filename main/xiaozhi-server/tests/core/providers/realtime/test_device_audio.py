import ctypes
import ctypes.util
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_real_opus_shim():
    path = ctypes.util.find_library("opus")
    if not path and Path("/opt/homebrew/lib/libopus.dylib").is_file():
        path = "/opt/homebrew/lib/libopus.dylib"
    if not path:
        raise unittest.SkipTest("libopus is not installed")
    lib = ctypes.CDLL(path)
    lib.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_encoder_create.restype = ctypes.c_void_p
    lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32]
    lib.opus_encode.restype = ctypes.c_int
    lib.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_decoder_create.restype = ctypes.c_void_p
    lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32, ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
    lib.opus_decode.restype = ctypes.c_int
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]

    class Encoder:
        def __init__(self, rate, channels, application):
            err = ctypes.c_int()
            self.handle = lib.opus_encoder_create(rate, channels, application, ctypes.byref(err))
            if err.value:
                raise RuntimeError(f"Opus encoder error {err.value}")

        def encode(self, pcm, frame_size):
            samples = (ctypes.c_int16 * frame_size).from_buffer_copy(pcm)
            out = (ctypes.c_ubyte * 8192)()
            size = lib.opus_encode(self.handle, samples, frame_size, out, len(out))
            if size < 0:
                raise RuntimeError(f"Opus encode error {size}")
            return bytes(out[:size])

        def __del__(self):
            if getattr(self, "handle", None):
                lib.opus_encoder_destroy(self.handle)

    class Decoder:
        def __init__(self, rate, channels):
            err = ctypes.c_int()
            self.handle = lib.opus_decoder_create(rate, channels, ctypes.byref(err))
            if err.value:
                raise RuntimeError(f"Opus decoder error {err.value}")

        def decode(self, packet, frame_size):
            encoded = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
            output = (ctypes.c_int16 * frame_size)()
            size = lib.opus_decode(self.handle, encoded, len(packet), output, frame_size, 0)
            if size < 0:
                raise RuntimeError(f"Opus decode error {size}")
            return bytes(output)[:size * 2]

        def __del__(self):
            if getattr(self, "handle", None):
                lib.opus_decoder_destroy(self.handle)

    return types.SimpleNamespace(Encoder=Encoder, Decoder=Decoder, APPLICATION_AUDIO=2049)


class DeviceAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shim = load_real_opus_shim()
        cls.patch = patch.dict(sys.modules, {"opuslib_next": cls.shim})
        cls.patch.start()
        cls.module = importlib.import_module("core.providers.realtime.device_audio")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("core.providers.realtime.device_audio", None)
        cls.patch.stop()

    def test_actual_opus_60ms_roundtrip_at_device_rate(self):
        codec = self.module.DeviceAudioCodec(input_rate=16000, output_rate=16000)
        pcm = b"\0\0" * 960
        packets = codec.encode_output(pcm)
        self.assertEqual(len(packets), 1)
        self.assertEqual(len(codec.decode_input(packets[0])), len(pcm))

    def test_interruption_discards_partial_audio(self):
        codec = self.module.DeviceAudioCodec(input_rate=16000, output_rate=16000)
        self.assertEqual(codec.encode_output(b"\0\0" * 400), [])
        codec.discard_output()
        self.assertEqual(codec.encode_output(b"\0\0" * 560), [])
        self.assertEqual(len(codec.encode_output(b"\0\0" * 400)), 1)

    def test_finish_pads_and_sends_final_short_frame(self):
        codec = self.module.DeviceAudioCodec(input_rate=16000, output_rate=16000)
        self.assertEqual(codec.encode_output(b"\0\0" * 320), [])
        packet = codec.finish_output()
        self.assertIsNotNone(packet)
        self.assertEqual(len(codec.decode_input(packet)), 1920)
        self.assertIsNone(codec.finish_output())


if __name__ == "__main__":
    unittest.main()
