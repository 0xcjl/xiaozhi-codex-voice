"""Loopback XiaoZhi WebSocket -> real ChatGPT-auth Codex WebRTC -> Opus.

Explicitly opt-in. No microphone, device, saved credentials or output audio.
"""

import argparse
import asyncio
import gc
import json
import secrets
import struct
from functools import partial
from pathlib import Path

import opuslib_next
import websockets

from core.auth import AuthManager
from core.providers.realtime.codex_session import CodexRealtimeSession
from core.providers.realtime.device_media_pipe import DeviceMediaPipe
from core.providers.realtime.service import RealtimeConnection, try_handle_realtime
from core.providers.realtime.xiaozhi_protocol import AudioPacket, pack_audio, unpack_audio


async def trial(workspace, pcm, index, *, turns=1, idle_seconds=0, console_config=None, device_id="synthetic-device", device_token=None, ota_url=None):
    status = {"run": index, "model": "gpt-6.1-terra", "account": "chatgpt", "synthetic_device": True,
              "user_transcript_matches": False, "assistant_transcript_matches": False,
              "audio_packets": 0, "audio_peak": 0, "turn_audio_peak": 0, "playback_starts": 0, "playback_stops": 0,
              "turns_verified": 0, "idle_seconds_between_turns": idle_seconds}
    active = set()
    pipes = []
    reader = None
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop_errors = []
    loop.set_exception_handler(lambda _, context: loop_errors.append(type(context.get("exception")).__name__))
    auth = AuthManager(secrets.token_urlsafe(32))
    config = {"server": {"auth": {"enabled": True}}, "codex_realtime": {
        "enabled": True, "device_ids": ["synthetic-device"], "workspace": str(workspace)}}
    listen_port = 0

    if console_config is not None:
        from urllib.parse import urlparse
        from config.manage_api_client import init_service
        config = json.loads(console_config.read_text())
        if urlparse(config.get("manager-api", {}).get("url", "")).hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("synthetic console test requires loopback management API")
        config["read_config_from_api"] = True
        config["server"] = {"auth": {"enabled": True}}
        if ota_url is not None:
            import httpx
            if urlparse(ota_url).hostname != "127.0.0.1":
                raise ValueError("synthetic OTA must use loopback")
            async with httpx.AsyncClient(trust_env=False, timeout=35) as client:
                response = await client.post(ota_url, headers={"Device-Id":device_id,"Client-Id":"synthetic-client"},
                    json={"application":{"version":"2.0.5"}})
                response.raise_for_status()
                discovery = response.json()
            if set(discovery) != {"websocket"}:
                raise ValueError("native discovery must not return firmware or MQTT")
            endpoint = urlparse(discovery["websocket"]["url"])
            if endpoint.scheme != "ws" or endpoint.hostname != "127.0.0.1" or not endpoint.port or endpoint.path != "/xiaozhi/v1/":
                raise ValueError("synthetic voice endpoint must use loopback")
            listen_port = endpoint.port
            device_token = discovery["websocket"]["token"]
            status["real_ota_discovery"] = True
        if not device_token or not device_token.startswith(("native1.", "native2.")):
            raise ValueError("console test requires an owner-issued native device token")
        auth = AuthManager(config["manager-api"]["secret"])
        init_service(config)
        status["native_token_scheme"] = device_token.split(".",1)[0]
        status["console_selection"] = True
        status["isolated_account_profile"] = True

    def observe(event):
        if not isinstance(event, dict):
            return
        if event.get("type") == "turn.done":
            turn = event.get("turn", {})
            text = turn.get("transcript", "")
            text = text.casefold() if isinstance(text, str) else ""
            if turn.get("role") == "user":
                status["user_transcript_matches"] = "hello" in text
            elif turn.get("role") == "assistant" and status["user_transcript_matches"]:
                status["assistant_transcript_matches"] = "hello" in text or "hi" in text.split()

    class ObservedQueue:
        def __init__(self, queue):
            self.queue = queue
        def __getattr__(self, name):
            return getattr(self.queue, name)
        async def get(self):
            event = await self.queue.get()
            observe(event)
            return event

    def make_pipe(*args, **kwargs):
        pipe = DeviceMediaPipe(*args, **kwargs)
        pipe.media.session = CodexRealtimeSession(workspace, model="gpt-6.1-terra", codex_home=kwargs.get("codex_home"), developer_instructions=(
            "This is an isolated synthetic voice test. Do not use tools, access files, browse, "
            "delegate tasks, or contact external services. Give a short spoken reply."))
        pipe.media.events = ObservedQueue(pipe.media.events)
        pipes.append(pipe)
        return pipe

    class ObservedConnection(RealtimeConnection):
        async def run(self):
            try:
                await super().run()
            except Exception as error:
                status["server_failure_type"] = type(error).__name__
                # Only the transport's explicitly sanitized state message is
                # safe to include. Never print arbitrary provider exceptions.
                text = str(error)
                if text.startswith("WebRTC not ready: peer=") or text == "device disconnected or realtime startup timed out":
                    status["startup_diagnostic"] = text
                raise

    async def handler(socket):
        await try_handle_realtime(socket, config, auth, active_devices=active,
                                 connection_factory=partial(ObservedConnection, make_pipe=make_pipe))

    try:
        async with websockets.serve(handler, "127.0.0.1", listen_port, max_size=16384, max_queue=16) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/xiaozhi/v1/"
            headers = {"device-id": device_id, "client-id": "synthetic-client", "protocol-version": "2",
                       "authorization": "Bearer " + (device_token or auth.generate_token("synthetic-client", device_id))}
            async with websockets.connect(url, additional_headers=headers) as socket:
                hello_started = loop.time()
                await socket.send(json.dumps({"type": "hello", "version": 2, "transport": "websocket", "audio_params": {
                    "format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
                hello = json.loads(await asyncio.wait_for(socket.recv(), 65))
                status["device_hello_ms"] = round((loop.time() - hello_started) * 1000, 1)
                if status["device_hello_ms"] >= 10000:
                    raise RuntimeError("device hello exceeded firmware deadline")
                if hello.get("type") != "hello":
                    raise RuntimeError("device handshake not acknowledged")
                decoder = opuslib_next.Decoder(16000, 1)
                playback_active = False

                async def receive():
                    nonlocal playback_active
                    async for message in socket:
                        if isinstance(message, str):
                            event = json.loads(message)
                            if event.get("type") == "tts":
                                playback_active = event.get("state") == "start"
                                key = "playback_starts" if playback_active else "playback_stops"
                                status[key] += 1
                        else:
                            if not playback_active:
                                raise RuntimeError("audio arrived outside device playback state")
                            decoded = decoder.decode(unpack_audio(2, message).opus, 960)
                            samples = struct.unpack("<" + "h" * (len(decoded) // 2), decoded)
                            status["audio_peak"] = max(status["audio_peak"], max(map(abs, samples), default=0))
                            status["turn_audio_peak"] = max(status["turn_audio_peak"], max(map(abs, samples), default=0))
                            status["audio_packets"] += 1

                reader = asyncio.create_task(receive())
                await socket.send(json.dumps({"type": "listen", "state": "start"}))
                encoder = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_AUDIO)
                for turn in range(turns):
                    if turn and idle_seconds:
                        print(json.dumps({"phase": "idle_before_next_turn", "seconds": idle_seconds}), flush=True)
                        await asyncio.sleep(idle_seconds)
                    stop_count = status["playback_stops"]
                    status["user_transcript_matches"] = False
                    status["assistant_transcript_matches"] = False
                    status["turn_audio_peak"] = 0
                    for offset in range(0, len(pcm), 1920):
                        chunk = pcm[offset:offset + 1920]
                        chunk += bytes(1920 - len(chunk))
                        await socket.send(pack_audio(2, AudioPacket(encoder.encode(chunk, 960))))
                        await asyncio.sleep(0.06)
                    async with asyncio.timeout(40):
                        while not (status["user_transcript_matches"] and status["assistant_transcript_matches"]
                                   and status["turn_audio_peak"] > 1000 and status["playback_stops"] > stop_count and not playback_active):
                            if reader.done():
                                reader.result()
                                raise RuntimeError("device socket closed before reply")
                            await asyncio.sleep(0.05)
                    status["turns_verified"] += 1
                await socket.send(json.dumps({"type": "goodbye"}))
                await socket.wait_closed()
        status["resources_released"] = not active and all(pipe.media.closed for pipe in pipes)
        if not status["resources_released"]:
            raise RuntimeError("connection resources were not released")
        status["connection_task_failures"] = [pipe.media.peer.connect_failure for pipe in pipes if pipe.media.peer.connect_failure]
        if status["connection_task_failures"]:
            raise RuntimeError("background connection task failed")
        gc.collect()
        await asyncio.sleep(0)
        status["event_loop_errors"] = loop_errors
        if loop_errors:
            raise RuntimeError("unhandled background connection failure")
        status["status"] = "passed"
    except Exception as error:
        status["status"] = "failed"
        status["failure_type"] = type(error).__name__
        raise
    finally:
        if reader:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        loop.set_exception_handler(previous_handler)
        print(json.dumps(status), flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--input-pcm", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=1, choices=range(1, 4))
    parser.add_argument("--turns", type=int, default=1, choices=range(1, 4))
    parser.add_argument("--idle-seconds", type=int, default=0, choices=range(601))
    parser.add_argument("--console-config", type=Path)
    parser.add_argument("--device-id", default="synthetic-device")
    parser.add_argument("--device-token-file", type=Path)
    parser.add_argument("--ota-url-file", type=Path)
    args = parser.parse_args()
    pcm = args.input_pcm.read_bytes()
    if not pcm or len(pcm) % 2 or len(pcm) > 16000 * 2 * 15:
        raise ValueError("expected a short mono 16 kHz s16 synthetic clip")
    for index in range(1, args.runs + 1):
        await trial(args.workspace.resolve(), pcm, index, turns=args.turns, idle_seconds=args.idle_seconds, console_config=args.console_config, device_id=args.device_id, device_token=args.device_token_file.read_text().strip() if args.device_token_file else None, ota_url=args.ota_url_file.read_text().strip() if args.ota_url_file else None)


if __name__ == "__main__":
    asyncio.run(main())
