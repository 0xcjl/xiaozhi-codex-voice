"""Authorized live test: synthetic XiaoZhi frames and an ephemeral Codex session.

Uses no physical device, microphone, user files, or saved output audio.
Playback is explicitly enabled by this test, not correlated to actual device turns.
"""

import argparse
import asyncio
import json
import struct
from pathlib import Path

import opuslib_next
from core.providers.realtime.codex_session import CodexRealtimeSession
from core.providers.realtime.device_media_pipe import DeviceMediaPipe
from core.providers.realtime.device_session import DeviceSession
from core.providers.realtime.xiaozhi_protocol import AudioPacket, pack_audio, unpack_audio


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--input-pcm", type=Path, required=True)
    args = parser.parse_args()
    pcm = args.input_pcm.read_bytes()
    if not pcm or len(pcm) % 2 or len(pcm) > 16000 * 2 * 15:
        raise ValueError("synthetic input must be short, mono 16 kHz s16 PCM")
    output = asyncio.Event()
    peak = 0
    raw_peak = 0
    raw_packets = 0
    first_audio_at = None
    last_audio_at = None
    started_at = asyncio.get_running_loop().time()
    packet_count = 0
    decoder = opuslib_next.Decoder(16000, 1)
    event_types = []
    event_sequence = []
    assistant_done = asyncio.Event()
    user_done = False
    response_started = False
    user_transcript_matches = False
    assistant_transcript_matches = False

    async def send(message):
        nonlocal peak, packet_count, raw_peak, raw_packets, first_audio_at, last_audio_at
        if not isinstance(message, bytes):
            return
        # Opus is stateful: decode every packet, including speech that arrives
        # before text-turn events. Record both paths rather than assuming sync.
        raw_packets += 1
        decoded = decoder.decode(unpack_audio(2, message).opus, 960)
        samples = struct.unpack("<" + "h" * (len(decoded) // 2), decoded)
        if samples:
            current_peak = max(map(abs, samples))
            raw_peak = max(raw_peak, current_peak)
            if current_peak > 1000:
                output.set()
                last_audio_at = round(asyncio.get_running_loop().time() - started_at, 3)
                if first_audio_at is None:
                    first_audio_at = last_audio_at
            if not response_started:
                return
            packet_count += 1
            peak = max(peak, current_peak)

    async def interrupt():
        pass

    def make_pipe(workspace, send_opus, **kwargs):
        pipe = DeviceMediaPipe(workspace, send_opus, **kwargs)
        pipe.media.session = CodexRealtimeSession(
            workspace, model="gpt-6.1-terra",
            developer_instructions="This is an isolated synthetic voice test. Do not use tools, access files, browse, delegate tasks, or contact external services. Give a short spoken reply.",
        )
        return pipe

    hello = {"type": "hello", "version": 2, "transport": "websocket", "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
    device = DeviceSession(args.workspace, send, interrupt, authenticated=True, make_pipe=make_pipe)
    collector = None
    try:
        await device.hello(hello, "2", "synthetic-device")

        async def collect_events():
            nonlocal user_done, response_started, user_transcript_matches, assistant_transcript_matches
            while True:
                event = await device.pipe.media.events.get()
                if isinstance(event, dict):
                    event_types.append(event.get("type", "unknown"))
                    event_sequence.append({"type": event.get("type", "unknown"), "role": event.get("turn", {}).get("role"),
                                           "at": round(asyncio.get_running_loop().time() - started_at, 3)})
                    kind, role = event.get("type"), event.get("turn", {}).get("role")
                    transcript = event.get("turn", {}).get("transcript", "")
                    transcript = transcript.casefold() if isinstance(transcript, str) else ""
                    if kind == "turn.done" and role == "user":
                        user_done = True
                        user_transcript_matches = "hello" in transcript
                    if kind == "turn.created" and role == "assistant" and user_done:
                        response_started = True
                    if kind == "turn.done" and role == "assistant" and response_started:
                        assistant_transcript_matches = "hello" in transcript or "hi" in transcript.split()
                        assistant_done.set()

        collector = asyncio.create_task(collect_events())
        await device.device_message({"type": "listen", "state": "start"})
        await device.output_start()
        encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
        for offset in range(0, len(pcm), 1920):
            chunk = pcm[offset:offset + 1920]
            if len(chunk) < 1920:
                chunk += bytes(1920 - len(chunk))
            await device.device_audio(pack_audio(2, AudioPacket(encoder.encode(chunk, 960))))
            await asyncio.sleep(0.06)
        try:
            await asyncio.wait_for(output.wait(), 40)
            await asyncio.wait_for(assistant_done.wait(), 30)
        except asyncio.TimeoutError:
            print(json.dumps({"status": "timeout", "output_peak": peak, "output_packets": packet_count,
                              "event_types": sorted(set(event_types)), "event_sequence": event_sequence,
                              "user_done": user_done, "response_started": response_started,
                              "raw_peak": raw_peak, "raw_packets": raw_packets,
                              "first_audio_at": first_audio_at, "last_audio_at": last_audio_at,
                              "transport_failure": device.pipe.media.failure}), flush=True)
            raise
        if not user_transcript_matches or not assistant_transcript_matches:
            print(json.dumps({"status": "transcript_not_verified", "user_transcript_matches": user_transcript_matches,
                              "assistant_transcript_matches": assistant_transcript_matches,
                              "raw_peak": raw_peak, "event_sequence": event_sequence}), flush=True)
            raise RuntimeError("synthetic input and reply transcripts were not verified")
        await asyncio.sleep(1)
        print(json.dumps({"model": "gpt-6.1-terra", "account": "chatgpt", "synthetic_device": True,
                          "device_protocol": 2, "output_audio_detected": True, "output_peak": peak,
                          "output_packets": packet_count, "assistant_turn_done": assistant_done.is_set(), "event_types": sorted(set(event_types)),
                          "event_sequence": event_sequence,
                          "raw_peak": raw_peak, "raw_packets": raw_packets,
                          "first_audio_at": first_audio_at, "last_audio_at": last_audio_at,
                          "user_transcript_matches": user_transcript_matches,
                          "assistant_transcript_matches": assistant_transcript_matches,
                          "transport_failure": device.pipe.media.failure}))
    finally:
        if collector:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
        await device.close()


if __name__ == "__main__":
    asyncio.run(main())
