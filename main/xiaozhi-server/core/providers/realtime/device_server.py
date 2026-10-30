"""Loopback-only native device ingress for the isolated console integration.

This entry point deliberately has no public bind option or ordinary-model
fallback. Deployment requires a separately reviewed TLS device ingress.
"""
import argparse
import asyncio
import json
from pathlib import Path

import websockets
from core.auth import AuthManager
from config.manage_api_client import init_service, manage_api_http_safe_close
from .service import try_handle_realtime,close_task_runtime


async def serve(config, port):
    settings = config.get("codex_realtime", {})
    secret = config.get("manager-api", {}).get("secret", "")
    if (settings.get("enabled") is not True or settings.get("console_selection") is not True
            or not isinstance(secret, str) or len(secret) < 24):
        raise ValueError("native console route and strong management credentials are required")
    config = {**config, "read_config_from_api": True, "server": {"auth": {"enabled": True}}}
    init_service(config)
    active = set()
    auth = AuthManager(secret)

    async def handle(socket):
        if socket.request.path != "/xiaozhi/v1/":
            await socket.close(code=1008, reason="Unsupported device endpoint")
            return
        if not await try_handle_realtime(socket, config, auth, active_devices=active):
            await socket.close(code=1008, reason="Native subscription selection is required")

    try:
        async with websockets.serve(handle, "127.0.0.1", port, compression=None, max_size=16384, max_queue=16):
            print("Isolated native device ingress listening on loopback", flush=True)
            await asyncio.Future()
    finally:
        await close_task_runtime(config)
        manage_api_http_safe_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18824)
    args = parser.parse_args()
    try:
        asyncio.run(serve(json.loads(args.config.read_text()), args.port))
    except KeyboardInterrupt:
        pass
