"""Explicit opt-in live test: synthetic audio only, ephemeral read-only session.

Run only when authorized; uses the locally signed-in ChatGPT Codex account.
Never reads a microphone or saves input/output audio or transcripts.
"""

import argparse
import asyncio
import json
import struct
from pathlib import Path

from core.providers.realtime.codex_media import CodexMediaTransport
from core.providers.realtime.codex_session import CodexRealtimeSession


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--input-pcm", type=Path, required=True)
    args = parser.parse_args()
    pcm = args.input_pcm.read_bytes()
    if not pcm or len(pcm) % 2 or len(pcm) > 16000 * 2 * 15:
        raise ValueError("synthetic input must be short, mono 16 kHz s16 PCM")
    heard = asyncio.Event()
    peak = 0
    event_types = []

    async def consume(data):
        nonlocal peak
        samples = struct.unpack("<" + "h" * (len(data) // 2), data)
        if samples:
            peak = max(peak, max(map(abs, samples)))
            if peak > 1000:
                heard.set()

    transport = CodexMediaTransport(args.workspace, consume, model="gpt-6.1-terra")
    transport.session = CodexRealtimeSession(
        args.workspace, model="gpt-6.1-terra",
        developer_instructions="This is an isolated synthetic voice test. Do not use tools, access files, browse, delegate tasks, or contact external services. Give a short spoken reply.",
    )

    async def collect_events():
        while True:
            event = await transport.events.get()
            if isinstance(event, dict):
                event_types.append(event.get("type", "unknown"))

    collector = None
    try:
        await transport.start()
        collector = asyncio.create_task(collect_events())
        for offset in range(0, len(pcm), 640):
            chunk = pcm[offset:offset + 640]
            if len(chunk) < 640:
                chunk += bytes(640 - len(chunk))
            transport.input.feed_pcm(chunk)
            await asyncio.sleep(0.02)
        await asyncio.wait_for(heard.wait(), 40)
        print(json.dumps({"model": transport.session.model, "account": "chatgpt", "synthetic_input": True,
                          "output_audio_detected": True, "output_peak": peak,
                          "event_types": sorted(set(event_types)), "transport_failure": transport.failure}))
    finally:
        if collector:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
        await transport.close()


if __name__ == "__main__":
    asyncio.run(main())
