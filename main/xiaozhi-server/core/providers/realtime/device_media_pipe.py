"""Experimental audio pipe used by the opt-in Codex service route.

The caller owns device authentication, WebSocket framing, TTS state messages,
playback pacing, acknowledgement correlation, and session reconnection.
"""

import asyncio

from .codex_media import CodexMediaTransport
from .device_audio import DeviceAudioCodec


class DeviceMediaPipe:
    def __init__(self, workspace, send_opus, model="gpt-6.1-terra", frame_ms=60, output_rate=16000, codex_home=None):
        self.codec = DeviceAudioCodec(input_rate=16000, output_rate=output_rate, frame_ms=frame_ms)
        self.send_opus = send_opus
        self.playback_enabled = False
        self.playback_generation = 0
        self.output_lock = asyncio.Lock()
        self.on_pcm = None
        self.media = CodexMediaTransport(workspace, self._receive_pcm, model=model, output_rate=output_rate, codex_home=codex_home)

    async def _receive_pcm(self, pcm):
        if self.on_pcm is not None:
            await self.on_pcm(pcm)
        else:
            await self._output_pcm(pcm)

    async def start(self):
        await self.media.start()

    def feed_device_opus(self, opus_packet: bytes):
        self.media.input.feed_pcm(self.codec.decode_input(opus_packet))

    def begin_output(self):
        self.playback_generation += 1
        self.codec.discard_output()
        self.playback_enabled = True

    def interrupt_output(self):
        self.playback_enabled = False
        self.playback_generation += 1
        self.codec.discard_output()

    async def finish_output(self):
        generation = self.playback_generation
        async with self.output_lock:
            if not self.playback_enabled or generation != self.playback_generation:
                return
            self.playback_enabled = False
            packet = self.codec.finish_output()
            if packet:
                await self.send_opus(packet)

    async def _output_pcm(self, pcm: bytes):
        generation = self.playback_generation
        async with self.output_lock:
            if not self.playback_enabled or generation != self.playback_generation:
                return
            for packet in self.codec.encode_output(pcm):
                if not self.playback_enabled or generation != self.playback_generation:
                    return
                await self.send_opus(packet)

    async def close(self):
        self.interrupt_output()
        await self.media.close()
