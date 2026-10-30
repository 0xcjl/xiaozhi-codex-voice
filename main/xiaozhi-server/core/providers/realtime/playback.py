"""Segment remote PCM for XiaoZhi playback independently of text events.

This detects activity in the model's output, never in the device microphone.
Abort suppresses old output until a quiet gap; upstream speech interruption
still depends on the realtime service hearing the caller's new audio.
"""

import struct
import time


class DevicePlayback:
    def __init__(self, device, pipe):
        self.device = device
        self.pipe = pipe
        self.ready = False
        self.blocked = False
        self.quiet_bytes = 0
        self.preroll = bytearray()
        self.last_pcm_at = None

    def block(self):
        self.blocked = True
        self.quiet_bytes = 0
        self.preroll.clear()

    def release(self):
        self.blocked = False
        self.quiet_bytes = 0
        self.preroll.clear()

    async def finish_if_idle(self,now=None):
        # Some realtime tracks stop emitting packets instead of sending silence.
        # A source gap may close a segment; it never cancels a user/agent turn.
        now=time.monotonic() if now is None else now
        if self.device.output_active and self.last_pcm_at is not None and now-self.last_pcm_at>=1.2:
            await self.device.output_finish()
            self.quiet_bytes=0;self.preroll.clear()

    async def feed(self, pcm):
        if not self.ready:
            return
        if len(pcm) % 2:
            raise ValueError("expected mono s16 PCM")
        self.last_pcm_at=time.monotonic()
        samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
        audible = bool(samples) and max(map(abs, samples)) > 128
        self.quiet_bytes = 0 if audible else self.quiet_bytes + len(pcm)
        if self.blocked:
            if self.quiet_bytes >= 6400:  # 200 ms at mono 16 kHz s16.
                self.blocked = False
                self.quiet_bytes = 0
            return
        if not self.device.output_active:
            self.preroll.extend(pcm)
            if len(self.preroll) > 5120:  # At most 160 ms, kept on the server.
                del self.preroll[:-5120]
            if not audible:
                return
            await self.device.output_start()
            if self.blocked:
                self.preroll.clear()
                return
            pcm = bytes(self.preroll)
            self.preroll.clear()
        await self.pipe._output_pcm(pcm)
        if self.quiet_bytes >= 25600:  # Preserve an 800 ms quiet tail.
            await self.device.output_finish()
            self.quiet_bytes = 0
