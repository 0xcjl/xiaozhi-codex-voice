"""Real loopback WebSocket and Opus; deterministic synthetic media peer."""

import asyncio
import json
import struct
import types
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

try:
    import websockets
    from core.providers.realtime.device_audio import DeviceAudioCodec
    HAVE_TRANSPORT = True
except ImportError:
    HAVE_TRANSPORT = False

from core.auth import AuthManager
from core.providers.realtime.service import RealtimeConnection, try_handle_realtime
from core.providers.realtime.xiaozhi_protocol import AudioPacket, pack_audio, unpack_audio


class EchoPipe:
    def __init__(self, workspace, send_opus, **kwargs):
        self.send_opus = send_opus
        self.codec = DeviceAudioCodec()
        self.on_pcm = None
        self.enabled = False
        self.closed = False
        self.frames = asyncio.Queue()
        self.task = None
        self.media = types.SimpleNamespace(
            failure=None, events=asyncio.Queue(maxsize=128),
            peer=types.SimpleNamespace(connectionState="connected"),
            channel=types.SimpleNamespace(readyState="open"),
            input=types.SimpleNamespace(clear_audio=lambda: None),
            session=types.SimpleNamespace(events=asyncio.Queue(maxsize=128), event_overflow=False, process=None))

    async def start(self):
        self.task = asyncio.create_task(self._echo())

    async def _echo(self):
        while True:
            await self.on_pcm(await self.frames.get())

    def feed_device_opus(self, packet):
        self.frames.put_nowait(self.codec.decode_input(packet))

    def begin_output(self):
        self.enabled = True

    def interrupt_output(self):
        self.enabled = False
        self.codec.discard_output()

    async def _output_pcm(self, pcm):
        if self.enabled:
            for packet in self.codec.encode_output(pcm):
                await self.send_opus(packet)

    async def finish_output(self):
        if self.enabled:
            packet = self.codec.finish_output()
            if packet:
                await self.send_opus(packet)
        self.enabled = False

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


@unittest.skipUnless(HAVE_TRANSPORT, "optional WebSocket and Opus dependencies are required")
class ServiceWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = AuthManager("synthetic-loopback-secret")
        self.config = {"server": {"auth": {"enabled": True}}, "codex_realtime": {
            "enabled": True, "device_ids": ["synthetic-device"], "workspace": str(Path.cwd())}}
        self.pipes = []
        self.active = set()
        def factory(*args, **kwargs):
            pipe = EchoPipe(*args, **kwargs)
            self.pipes.append(pipe)
            return pipe
        async def handle(socket):
            await try_handle_realtime(socket, self.config, self.auth, active_devices=self.active,
                                     connection_factory=partial(RealtimeConnection, make_pipe=factory))
        self.server = await websockets.serve(handle, "127.0.0.1", 0, max_size=16384, max_queue=16)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/xiaozhi/v1/"
        self.headers = {"device-id": "synthetic-device", "client-id": "synthetic-client", "protocol-version": "2",
                        "authorization": "Bearer " + self.auth.generate_token("synthetic-client", "synthetic-device")}

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.assertEqual(self.active, set())

    async def test_real_socket_auth_hello_opus_playback_and_cleanup(self):
        codec = DeviceAudioCodec()
        decoder = DeviceAudioCodec()
        hello = {"type": "hello", "version": 2, "transport": "websocket", "audio_params": {
            "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
        async with websockets.connect(self.url, additional_headers=self.headers) as socket:
            await socket.send(json.dumps(hello))
            self.assertEqual(json.loads(await socket.recv())["type"], "hello")
            await socket.send(json.dumps({"type": "listen", "state": "start"}))
            speech = struct.pack("<960h", *([5000, -5000] * 480))
            for packet in codec.encode_output(speech):
                await socket.send(pack_audio(2, AudioPacket(packet)))
            self.assertEqual(json.loads(await socket.recv())["state"], "start")
            output = await socket.recv()
            decoded = decoder.decode_input(unpack_audio(2, output).opus)
            self.assertGreater(max(map(abs, struct.unpack("<960h", decoded))), 128)
            for _ in range(16):
                for packet in codec.encode_output(bytes(1920)):
                    await socket.send(pack_audio(2, AudioPacket(packet)))
            async with asyncio.timeout(3):
                while True:
                    message = await socket.recv()
                    if isinstance(message, str) and json.loads(message).get("state") == "stop":
                        break
            # App-server notifications must be consumed as well as WebRTC events.
            for _ in range(256):
                await asyncio.wait_for(self.pipes[0].media.session.events.put({"method": "thread/realtime/transcript/delta"}), 1)
            await socket.send(json.dumps({"type": "goodbye"}))
            await socket.wait_closed()
        self.assertTrue(self.pipes[0].closed)

    async def test_missing_token_never_opens_codex(self):
        headers = dict(self.headers)
        headers.pop("authorization")
        async with websockets.connect(self.url, additional_headers=headers) as socket:
            with self.assertRaises(websockets.exceptions.ConnectionClosedError) as caught:
                await socket.recv()
            self.assertEqual(caught.exception.rcvd.code, 1008)
        self.assertEqual(self.pipes, [])

    async def test_abort_stops_old_audio_and_allows_next_output(self):
        codec = DeviceAudioCodec()
        speech = struct.pack("<960h", *([5000, -5000] * 480))
        async with websockets.connect(self.url, additional_headers=self.headers) as socket:
            await socket.send(json.dumps({"type": "hello", "version": 2, "transport": "websocket", "audio_params": {
                "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
            await socket.recv()
            async def frame(pcm):
                for packet in codec.encode_output(pcm):
                    await socket.send(pack_audio(2, AudioPacket(packet)))
            await frame(speech)
            self.assertEqual(json.loads(await socket.recv())["state"], "start")
            await socket.recv()
            await socket.send(json.dumps({"type": "abort"}))
            async with asyncio.timeout(2):
                while True:
                    message = await socket.recv()
                    if isinstance(message, str) and json.loads(message).get("state") == "stop":
                        break
            await frame(speech)
            for _ in range(16):
                await frame(bytes(1920))
            await frame(speech)
            async with asyncio.timeout(2):
                self.assertEqual(json.loads(await socket.recv())["state"], "start")
                self.assertIsInstance(await socket.recv(), bytes)
            await socket.send(json.dumps({"type": "goodbye"}))
            await socket.wait_closed()

    async def test_disconnect_closes_media(self):
        async with websockets.connect(self.url, additional_headers=self.headers) as socket:
            await socket.send(json.dumps({"type": "hello", "version": 2, "transport": "websocket", "audio_params": {
                "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
            await socket.recv()
        async with asyncio.timeout(2):
            while self.active:
                await asyncio.sleep(0.01)
        self.assertTrue(self.pipes[0].closed)

    async def test_disconnect_during_startup_cancels_and_closes_media(self):
        started = asyncio.Event()
        async def start(_):
            started.set()
            await asyncio.Event().wait()
        with patch.object(EchoPipe, "start", start), self.assertLogs("core.providers.realtime.service", level="WARNING"):
            async with websockets.connect(self.url, additional_headers=self.headers) as socket:
                await socket.send(json.dumps({"type": "hello", "version": 2, "transport": "websocket", "audio_params": {
                    "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
                await asyncio.wait_for(started.wait(), 2)
            async with asyncio.timeout(2):
                while self.active:
                    await asyncio.sleep(0.01)
        self.assertTrue(self.pipes[0].closed)

    async def test_backend_failure_closes_device_and_releases_media(self):
        with self.assertLogs("core.providers.realtime.service", level="WARNING"):
            async with websockets.connect(self.url, additional_headers=self.headers) as socket:
                await socket.send(json.dumps({"type": "hello", "version": 2, "transport": "websocket", "audio_params": {
                    "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
                await socket.recv()
                self.pipes[0].media.failure = "synthetic transport failure"
                with self.assertRaises(websockets.exceptions.ConnectionClosedError):
                    await asyncio.wait_for(socket.recv(), 2)
            async with asyncio.timeout(2):
                while self.active:
                    await asyncio.sleep(0.01)
        self.assertTrue(self.pipes[0].closed)

    async def test_fast_hello_preserves_audio_while_backend_starts(self):
        release = asyncio.Event()
        original_start = EchoPipe.start
        async def start(pipe):
            await release.wait()
            await original_start(pipe)
        codec = DeviceAudioCodec()
        with patch.object(EchoPipe, "start", start):
            async with websockets.connect(self.url, additional_headers=self.headers) as socket:
                await socket.send(json.dumps({"type": "hello", "version": 2, "transport": "websocket", "audio_params": {
                    "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
                self.assertEqual(json.loads(await asyncio.wait_for(socket.recv(), 0.5))["type"], "hello")
                self.assertIsNone(self.pipes[0].task)
                for packet in codec.encode_output(struct.pack("<960h", *([5000, -5000] * 480))):
                    await socket.send(pack_audio(2, AudioPacket(packet)))
                release.set()
                async with asyncio.timeout(2):
                    self.assertEqual(json.loads(await socket.recv())["state"], "start")
                    self.assertIsInstance(await socket.recv(), bytes)
                await socket.send(json.dumps({"type": "goodbye"}))
                await socket.wait_closed()

    async def test_startup_buffer_overflow_closes_without_waiting_for_backend(self):
        async def start(_):
            await asyncio.Event().wait()
        with patch.object(EchoPipe, "start", start):
            async with websockets.connect(self.url, additional_headers=self.headers) as socket:
                await socket.send(json.dumps({"type": "hello", "version": 2, "transport": "websocket", "audio_params": {
                    "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
                await socket.recv()
                for _ in range(167):
                    await socket.send(pack_audio(2, AudioPacket(b"x")))
                with self.assertRaises(websockets.exceptions.ConnectionClosedError) as caught:
                    await asyncio.wait_for(socket.recv(), 2)
                self.assertEqual(caught.exception.rcvd.code, 1013)
        self.assertTrue(self.pipes[0].closed)
