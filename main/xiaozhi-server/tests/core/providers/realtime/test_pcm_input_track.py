import importlib
import sys
import types
import unittest
from unittest.mock import patch


class FakeMediaStreamTrack:
    def __init__(self):
        self.readyState = "live"

    def stop(self):
        self.readyState = "ended"


class FakePlane:
    buffer_size = 640

    def update(self, data):
        self.data = data


class FakeFrame:
    def __init__(self, **kwargs):
        self.planes = [FakePlane()]


class FakeMediaStreamError(Exception):
    pass


class PcmTrackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module_name = "core.providers.realtime.codex_media"
        self.previous_module = sys.modules.pop(self.module_name, None)
        fake_aiortc = types.ModuleType("aiortc")
        fake_aiortc.MediaStreamTrack = FakeMediaStreamTrack
        fake_aiortc.RTCPeerConnection = object
        fake_aiortc.RTCSessionDescription = object
        fake_media = types.ModuleType("aiortc.mediastreams")
        fake_media.MediaStreamError = FakeMediaStreamError
        fake_av = types.ModuleType("av")
        fake_av.AudioFrame = FakeFrame
        fake_audio = types.ModuleType("av.audio")
        fake_resampler = types.ModuleType("av.audio.resampler")
        fake_resampler.AudioResampler = object
        self.modules = patch.dict(sys.modules, {"aiortc": fake_aiortc, "aiortc.mediastreams": fake_media, "av": fake_av, "av.audio": fake_audio, "av.audio.resampler": fake_resampler,
                                               "core.providers.realtime.managed_peer": types.SimpleNamespace(ManagedPeerConnection=object)})
        self.modules.start()
        self.module = importlib.import_module(self.module_name)

    def tearDown(self):
        sys.modules.pop(self.module_name, None)
        self.modules.stop()
        if self.previous_module is not None:
            sys.modules[self.module_name] = self.previous_module

    async def test_silence_when_no_audio_then_monotonic_pcm(self):
        track = self.module.PcmInputTrack()
        self.assertEqual((await track.recv()).planes[0].data, bytes(640))
        track.feed_pcm(b"\1\0" * 320)
        frame = await track.recv()
        self.assertEqual(frame.planes[0].data, b"\1\0" * 320)
        self.assertEqual(frame.pts, 320)
        track.stop()

    async def test_end_track_does_not_return_more_audio(self):
        track = self.module.PcmInputTrack()
        track.stop()
        with self.assertRaises(FakeMediaStreamError):
            await track.recv()

    async def test_buffer_bound_discards_oldest(self):
        track = self.module.PcmInputTrack()
        for value in range(31):
            track.feed_pcm(bytes([value, 0]) * 320)
        self.assertEqual(track.dropped_chunks, 1)
        self.assertEqual((await track.recv()).planes[0].data, b"\1\0" * 320)
        track.stop()
