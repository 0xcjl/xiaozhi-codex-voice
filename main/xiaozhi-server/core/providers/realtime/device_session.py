"""Experimental device adapter for the opt-in realtime service.

The caller authenticates the device and controls playback boundaries.
"""

from pathlib import Path
import asyncio

from .xiaozhi_protocol import AudioPacket, pack_audio, parse_hello, unpack_audio


class DeviceSession:
    def __init__(self, workspace, send, interrupt_codex, *, authenticated, make_pipe=None):
        if authenticated is not True:
            raise PermissionError("device must be authenticated before starting realtime")
        self.workspace = Path(workspace)
        self.send = send
        self.interrupt_codex = interrupt_codex
        self.make_pipe = make_pipe
        self.pipe = None
        self.version = None
        self.session_id = None
        self.output_active = False
        self.output_generation = 0
        self.closed = False

    async def hello(self, message, protocol_header, session_id):
        if self.pipe or self.closed:
            raise RuntimeError("session is already initialized or closed")
        version, rate, frame_ms = parse_hello(message)
        if protocol_header != str(version) or rate != 16000 or not session_id:
            raise ValueError("unsupported or inconsistent device handshake")
        self.version, self.session_id = version, session_id
        if self.make_pipe is None:
            from .device_media_pipe import DeviceMediaPipe
            self.make_pipe = DeviceMediaPipe
        try:
            await self.send({"type": "hello", "transport": "websocket", "session_id": session_id,
                             "audio_params": {"format": "opus", "sample_rate": rate, "channels": 1, "frame_duration": frame_ms}})
            for attempt in range(3):
                self.pipe = self.make_pipe(self.workspace, self._send_opus, frame_ms=frame_ms, output_rate=rate)
                try:
                    await self.pipe.start()
                    break
                except TimeoutError:
                    await self.pipe.close()
                    self.pipe = None
                    if attempt==2:
                        raise
                    await asyncio.sleep(.25*(attempt+1))
        except BaseException:
            if self.pipe:
                await self.pipe.close()
            self.pipe = None
            self.closed = True
            raise

    async def device_message(self, message):
        if not self.pipe or self.closed:
            raise RuntimeError("device session is not ready")
        kind = message.get("type")
        if kind == "abort":
            await self.interrupt()
        elif kind == "listen":
            if message.get("state") not in ("start", "stop", "detect"):
                raise ValueError("unsupported listening state")
        else:
            raise ValueError("unsupported device message")

    async def device_audio(self, frame):
        if not self.pipe or self.closed:
            raise RuntimeError("device session is not ready")
        self.pipe.feed_device_opus(unpack_audio(self.version, frame).opus)

    async def _send_opus(self, packet):
        if self.output_active and not self.closed:
            await self.send(pack_audio(self.version, AudioPacket(packet)))

    async def output_start(self):
        if not self.pipe or self.closed or self.output_active:
            raise RuntimeError("cannot start device output")
        self.output_generation += 1
        generation = self.output_generation
        self.output_active = True
        try:
            await self.send({"type": "tts", "state": "start", "session_id": self.session_id})
        except BaseException:
            if generation == self.output_generation:
                self.output_active = False
                self.output_generation += 1
                self.pipe.interrupt_output()
            raise
        if self.output_active and generation == self.output_generation and not self.closed:
            self.pipe.begin_output()

    async def output_finish(self):
        if not self.output_active:
            return
        generation = self.output_generation
        await self.pipe.finish_output()
        if not self.output_active or generation != self.output_generation:
            return
        self.output_active = False
        await self.send({"type": "tts", "state": "stop", "session_id": self.session_id})

    async def interrupt(self):
        if self.output_active:
            self.output_active = False
            self.output_generation += 1
            self.pipe.interrupt_output()
            await self.send({"type": "tts", "state": "stop", "session_id": self.session_id})
        await self.interrupt_codex()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.output_active = False
        if self.pipe:
            await self.pipe.close()
