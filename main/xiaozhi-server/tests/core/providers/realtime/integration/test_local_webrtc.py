"""No microphone, no Codex login, and no external ICE servers."""

import asyncio
import math
import struct
import unittest
import json
from pathlib import Path
from unittest.mock import patch

try:
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from core.providers.realtime.managed_peer import ManagedPeerConnection
    HAVE_MEDIA = True
except ImportError:
    HAVE_MEDIA = False


@unittest.skipUnless(HAVE_MEDIA, "optional aiortc and PyAV dependencies are not installed")
class LocalWebRTCTests(unittest.IsolatedAsyncioTestCase):
    async def test_offer_contains_audio_control_and_bundle_group(self):
        from core.providers.realtime.codex_media import CodexMediaTransport
        async def consume(_):
            pass
        transport = CodexMediaTransport(Path.cwd(), consume)
        try:
            offer = await transport.peer.createOffer()
            self.assertIn("m=audio", offer.sdp)
            self.assertIn("m=application", offer.sdp)
            bundle = next(line for line in offer.sdp.splitlines() if line.startswith("a=group:BUNDLE"))
            self.assertEqual(len(bundle.split()), 3)
        finally:
            await transport.close()

    async def test_missing_session_started_fails_closed(self):
        from core.providers.realtime import codex_media

        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))

        @peer.on("track")
        def echo_track(track):
            if track.kind == "audio":
                peer.addTrack(track)

        class FakeCodexSession:
            def __init__(self, workspace, model, codex_home=None):
                pass

            async def start(self, offer):
                await peer.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
                await peer.setLocalDescription(await peer.createAnswer())
                return peer.localDescription.sdp

            async def close(self):
                pass

        async def consume(pcm):
            pass

        with patch.object(codex_media, "CodexRealtimeSession", FakeCodexSession), patch.object(
            codex_media, "RTCPeerConnection", lambda: ManagedPeerConnection(RTCConfiguration(iceServers=[]))
        ):
            transport = codex_media.CodexMediaTransport(Path.cwd(), consume, startup_timeout=0.3)
            try:
                with self.assertRaises(TimeoutError):
                    await transport.start()
            finally:
                await transport.close()
                await peer.close()

    async def test_synthetic_pcm_roundtrips_between_local_peers(self):
        from core.providers.realtime import codex_media

        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))

        @peer.on("track")
        def echo_track(track):
            if track.kind == "audio":
                peer.addTrack(track)

        @peer.on("datachannel")
        def session_channel(channel):
            channel.send(json.dumps({"type": "session.started"}))

        class FakeCodexSession:
            def __init__(self, workspace, model, codex_home=None):
                self.model = model

            async def start(self, offer):
                await peer.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
                await peer.setLocalDescription(await peer.createAnswer())
                return peer.localDescription.sdp

            async def close(self):
                pass

        received = asyncio.Event()

        async def consume(pcm):
            samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
            if samples and max(map(abs, samples)) > 100:
                received.set()

        with patch.object(codex_media, "CodexRealtimeSession", FakeCodexSession), patch.object(
            codex_media, "RTCPeerConnection", lambda: ManagedPeerConnection(RTCConfiguration(iceServers=[]))
        ):
            transport = codex_media.CodexMediaTransport(Path.cwd(), consume)
            try:
                await transport.start()
                for chunk in range(80):
                    pcm = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * (chunk * 320 + i) / 16000))) for i in range(320))
                    transport.input.feed_pcm(pcm)
                    if received.is_set():
                        break
                    await asyncio.sleep(0.02)
                await asyncio.wait_for(received.wait(), 8)
                self.assertIsNone(transport.failure)
            finally:
                await transport.close()
                await peer.close()

    async def test_device_opus_to_webrtc_and_back_without_hardware(self):
        import opuslib_next
        from core.providers.realtime import codex_media
        from core.providers.realtime.device_session import DeviceSession
        from core.providers.realtime.xiaozhi_protocol import AudioPacket, pack_audio, unpack_audio

        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))

        @peer.on("track")
        def echo_track(track):
            if track.kind == "audio":
                peer.addTrack(track)

        @peer.on("datachannel")
        def session_channel(channel):
            channel.send(json.dumps({"type": "session.started"}))

        class FakeCodexSession:
            def __init__(self, workspace, model, codex_home=None):
                self.model = model

            async def start(self, offer):
                await peer.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
                await peer.setLocalDescription(await peer.createAnswer())
                return peer.localDescription.sdp

            async def close(self):
                pass

        output = asyncio.Event()
        decoder = opuslib_next.Decoder(16000, 1)
        events = []

        async def send(message):
            if isinstance(message, dict):
                events.append(message.get("state", message.get("type")))
                return
            pcm = decoder.decode(unpack_audio(2, message).opus, 960)
            samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
            if samples and max(map(abs, samples)) > 100:
                output.set()

        async def interrupt():
            events.append("interrupt")

        hello = {"type": "hello", "version": 2, "transport": "websocket", "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
        with patch.object(codex_media, "CodexRealtimeSession", FakeCodexSession), patch.object(
            codex_media, "RTCPeerConnection", lambda: ManagedPeerConnection(RTCConfiguration(iceServers=[]))
        ):
            device = DeviceSession(Path.cwd(), send, interrupt, authenticated=True)
            try:
                await device.hello(hello, "2", "test-session")
                await device.device_message({"type": "listen", "state": "start"})
                await device.output_start()
                encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
                for chunk in range(40):
                    pcm = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * (chunk * 960 + i) / 16000))) for i in range(960))
                    opus = encoder.encode(pcm, 960)
                    await device.device_audio(pack_audio(2, AudioPacket(opus)))
                    if output.is_set():
                        break
                    await asyncio.sleep(0.06)
                await asyncio.wait_for(output.wait(), 8)
                await device.device_message({"type": "abort"})
                self.assertEqual(events, ["hello", "start", "stop", "interrupt"])
            finally:
                await device.close()
                await peer.close()


if __name__ == "__main__":
    unittest.main()
