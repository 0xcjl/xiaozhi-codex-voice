import asyncio
import importlib
import sys
import types
import unittest
from unittest.mock import patch


class FakeCodec:
    def __init__(self, input_rate, output_rate, frame_ms):
        self.input_rate, self.output_rate, self.frame_ms = input_rate, output_rate, frame_ms

    def discard_output(self):
        pass

    def encode_output(self, pcm):
        return [b"one", b"two"]

    def finish_output(self):
        return b"tail"

    def decode_input(self, opus):
        return b"pcm"


class FakeMedia:
    def __init__(self, workspace, on_pcm, model, output_rate=24000, codex_home=None):
        self.output_rate = output_rate
        self.input = types.SimpleNamespace(feed_pcm=lambda pcm: None)

    async def close(self):
        pass


class DeviceMediaPipeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.modules = patch.dict(sys.modules, {
            "core.providers.realtime.codex_media": types.SimpleNamespace(CodexMediaTransport=FakeMedia),
            "core.providers.realtime.device_audio": types.SimpleNamespace(DeviceAudioCodec=FakeCodec),
        })
        self.modules.start()
        self.module = importlib.import_module("core.providers.realtime.device_media_pipe")

    def tearDown(self):
        sys.modules.pop("core.providers.realtime.device_media_pipe", None)
        self.modules.stop()

    async def test_default_output_matches_16000_hz_device_handshake(self):
        pipe = self.module.DeviceMediaPipe("/tmp", lambda packet: None)
        self.assertEqual(pipe.codec.output_rate, 16000)
        self.assertEqual(pipe.media.output_rate, 16000)

    async def test_interruption_drops_remaining_packets(self):
        sent = []
        pipe = None

        async def send(packet):
            sent.append(packet)
            pipe.interrupt_output()

        pipe = self.module.DeviceMediaPipe("/tmp", send)
        pipe.begin_output()
        await pipe._output_pcm(b"samples")
        self.assertEqual(sent, [b"one"])
        await pipe._output_pcm(b"later samples")
        self.assertEqual(sent, [b"one"])

    async def test_normal_finish_sends_tail_but_abort_does_not(self):
        sent = []

        async def send(packet):
            sent.append(packet)

        pipe = self.module.DeviceMediaPipe("/tmp", send)
        pipe.begin_output()
        await pipe.finish_output()
        self.assertEqual(sent, [b"tail"])
        pipe.begin_output()
        pipe.interrupt_output()
        await pipe.finish_output()
        self.assertEqual(sent, [b"tail"])

    async def test_finish_waits_for_inflight_audio_before_sending_tail(self):
        started, release = asyncio.Event(), asyncio.Event()
        sent = []

        async def send(packet):
            sent.append(packet)
            if packet == b"one":
                started.set()
                await release.wait()

        pipe = self.module.DeviceMediaPipe("/tmp", send)
        pipe.begin_output()
        output = asyncio.create_task(pipe._output_pcm(b"samples"))
        await started.wait()
        finish = asyncio.create_task(pipe.finish_output())
        await asyncio.sleep(0)
        self.assertEqual(sent, [b"one"])
        release.set()
        await asyncio.gather(output, finish)
        self.assertEqual(sent, [b"one", b"two", b"tail"])


if __name__ == "__main__":
    unittest.main()
