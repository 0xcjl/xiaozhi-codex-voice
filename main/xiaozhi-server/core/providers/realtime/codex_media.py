"""Experimental WebRTC audio transport for the opt-in Codex device route.

Requires optional aiortc and PyAV dependencies. Audio buffers are in memory.
The caller must enforce device authentication, playback state and interruption.
"""

import asyncio
import json
import logging
import time
from fractions import Fraction

try:
    from aiortc import MediaStreamTrack, RTCSessionDescription
    from .managed_peer import ManagedPeerConnection as RTCPeerConnection
    from aiortc.mediastreams import MediaStreamError
    from av import AudioFrame
    from av.audio.resampler import AudioResampler
except ImportError:
    MediaStreamTrack = object
    RTCPeerConnection = RTCSessionDescription = AudioFrame = AudioResampler = MediaStreamError = None

from .codex_session import CodexRealtimeSession


async def wait_transport_ready(peer, channel, timeout=20, interval=0.05, phase_timeout=8):
    """Do not submit audio until both the media path and control channel open."""
    deadline = asyncio.get_running_loop().time() + timeout
    previous_phase=None
    phase_deadline=deadline
    while True:
        if getattr(peer, "connect_failure", None):
            raise RuntimeError("WebRTC connection task failed")
        if peer.connectionState == "connected" and channel.readyState == "open":
            return
        if peer.connectionState in ("failed", "closed") or channel.readyState in ("closing", "closed"):
            raise RuntimeError(f"WebRTC connection failed: peer={peer.connectionState}, channel={channel.readyState}")
        sctp = getattr(peer, "sctp", None)
        dtls = getattr(sctp, "transport", None)
        phase=('transport' if sctp is None else
               'ice' if getattr(peer,'iceConnectionState',None) not in ('connected','completed') else
               'dtls' if dtls.state!='connected' else
               'sctp' if sctp.state!='connected' else 'data_channel')
        now=asyncio.get_running_loop().time()
        if phase!=previous_phase:
            previous_phase=phase;phase_deadline=now+phase_timeout
        if now >= min(deadline,phase_deadline):
            raise TimeoutError(
                f"WebRTC not ready: phase={phase}, peer={peer.connectionState}, channel={channel.readyState}, "
                f"sctp={getattr(sctp, 'state', 'unavailable')}, dtls={getattr(dtls, 'state', 'unavailable')}"
            )
        await asyncio.sleep(interval)


class PcmInputTrack(MediaStreamTrack):
    """Send mono 16 kHz PCM in 20 ms chunks, bounded to 600 ms."""

    kind = "audio"
    rate = 16000
    samples = 320
    chunk_bytes = 640

    def __init__(self):
        if AudioFrame is None:
            raise RuntimeError("Optional aiortc and PyAV dependencies are required")
        super().__init__()
        self.queue = asyncio.Queue(maxsize=30)
        self.pending = bytearray()
        self.pts = 0
        self.next_frame = None
        self.dropped_chunks = 0

    def feed_pcm(self, pcm: bytes):
        if len(pcm) % 2:
            raise ValueError("PCM must contain 16-bit samples")
        self.pending.extend(pcm)
        while len(self.pending) >= self.chunk_bytes:
            chunk = bytes(self.pending[:self.chunk_bytes])
            del self.pending[:self.chunk_bytes]
            if self.queue.full():
                self.queue.get_nowait()
                self.dropped_chunks += 1
            self.queue.put_nowait(chunk)

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        now = time.monotonic()
        if self.next_frame is None or now - self.next_frame > 0.1:
            self.next_frame = now
        await asyncio.sleep(max(0, self.next_frame - now))
        if self.readyState != "live":
            raise MediaStreamError
        self.next_frame += 0.02
        try:
            pcm = self.queue.get_nowait()
        except asyncio.QueueEmpty:
            pcm = bytes(self.chunk_bytes)
        frame = AudioFrame(format="s16", layout="mono", samples=self.samples)
        plane = frame.planes[0]
        plane.update(pcm + bytes(plane.buffer_size - len(pcm)))
        frame.sample_rate = self.rate
        frame.pts = self.pts
        frame.time_base = Fraction(1, self.rate)
        self.pts += self.samples
        return frame

    def clear_audio(self):
        self.pending.clear()
        while not self.queue.empty():
            self.queue.get_nowait()


class CodexMediaTransport:
    """Negotiate a Codex WebRTC stream and expose PCM and control events."""

    def __init__(self, workspace, on_pcm, model="gpt-6.1-terra", output_rate=16000, startup_timeout=20, codex_home=None):
        if RTCPeerConnection is None:
            raise RuntimeError("Optional aiortc and PyAV dependencies are required")
        self.session = CodexRealtimeSession(workspace, model=model, codex_home=codex_home)
        self.on_pcm = on_pcm
        self.output_rate = output_rate
        self.startup_timeout = startup_timeout
        self.session_started = asyncio.Event()
        self.peer = RTCPeerConnection()
        self.input = PcmInputTrack()
        self.events = asyncio.Queue(maxsize=128)
        self.output_tasks = set()
        self.failure = None
        self.closed = False
        self.peer.addTrack(self.input)
        self.channel = self.peer.createDataChannel("oai-events")

        @self.channel.on("message")
        def on_message(message):
            if not isinstance(message, str):
                return
            try:
                event = json.loads(message)
                if isinstance(event, dict) and event.get("type") == "session.started":
                    self.session_started.set()
                self.events.put_nowait(event)
            except json.JSONDecodeError:
                self.failure = "invalid realtime event"
            except asyncio.QueueFull:
                self.failure = "realtime event queue overflow"
                asyncio.create_task(self.close())

        @self.peer.on("track")
        def on_track(track):
            if track.kind == "audio":
                task = asyncio.create_task(self._receive_audio(track))
                self.output_tasks.add(task)
                task.add_done_callback(self._audio_finished)

    def _audio_finished(self, task):
        self.output_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.failure = "realtime audio receive failed"

    async def _receive_audio(self, track):
        resampler = AudioResampler(format="s16", layout="mono", rate=self.output_rate)
        while not self.closed:
            frame = await track.recv()
            for converted in resampler.resample(frame):
                pcm = bytes(converted.planes[0])[:converted.samples * 2]
                await self.on_pcm(pcm)

    async def start(self):
        stage='offer'
        try:
            offer = await self.peer.createOffer()
            await self.peer.setLocalDescription(offer)
            stage='codex_sdp'
            answer = await self.session.start(self.peer.localDescription.sdp)
            stage='transport'
            await self.peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
            await wait_transport_ready(self.peer, self.channel, timeout=self.startup_timeout)
            stage='session_started'
            await asyncio.wait_for(self.session_started.wait(), self.startup_timeout)
        except TimeoutError:
            sctp=getattr(self.peer,'sctp',None)
            logging.getLogger(__name__).warning('Realtime startup timeout: stage=%s peer=%s channel=%s sctp=%s association=%s',
                stage,self.peer.connectionState,self.channel.readyState,
                getattr(sctp,'state','unavailable'),getattr(getattr(sctp,'_association_state',None),'name','unavailable'))
            await self.close()
            raise
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.input.clear_audio()
        self.input.stop()
        for task in self.output_tasks:
            task.cancel()
        await asyncio.gather(*self.output_tasks, return_exceptions=True)
        try:
            await self.peer.close()
        finally:
            await self.session.close()
