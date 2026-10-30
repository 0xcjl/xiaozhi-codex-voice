"""Private runtime OAuth control: shared server secret, no browser credentials."""

import asyncio
import hmac
import json
from pathlib import Path
from aiohttp import web
from .account_profiles import AccountProfiles, AccountProfileError


def install_account_routes(app, config, *, manager=None):
    settings = config.get("codex_realtime", {})
    if settings.get("account_control", {}).get("enabled") is not True:
        return None
    secret = config.get("manager-api", {}).get("secret", "")
    root = settings.get("account_profiles_dir")
    workspace = settings.get("workspace")
    if not isinstance(secret, str) or len(secret) < 24 or not root or not workspace:
        raise ValueError("account control requires a strong secret and isolated profile directory")
    manager = manager or AccountProfiles(root, Path(workspace))
    allowed = {str(owner) for owner in settings.get("allowed_user_ids", [])}

    async def handle(request):
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + secret).encode()):
            return web.json_response({"error": "unauthorized"}, status=401, headers={"Cache-Control": "no-store"})
        if request.content_length is not None and request.content_length > 4096:
            raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=request.content_length)
        try:
            payload = bytearray()
            async for chunk in request.content.iter_chunked(4096):
                payload.extend(chunk)
                if len(payload) > 4096:
                    raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(payload))
            body = json.loads(payload)
            if not isinstance(body, dict) or set(body) != {"ownerId", "connectionId"}:
                raise ValueError()
            owner, connection = body["ownerId"], body["connectionId"]
            if str(owner) not in allowed:
                return web.json_response({"error": "runtime_access_denied"}, status=403, headers={"Cache-Control": "no-store"})
            action = request.match_info["action"]
            if action not in ("status", "start", "cancel", "refresh", "reauthorize"):
                raise web.HTTPNotFound()
            result = await getattr(manager, action)(owner, connection)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, AccountProfileError):
            return web.json_response({"error": "invalid_account_request"}, status=400, headers={"Cache-Control": "no-store"})
        except asyncio.TimeoutError:
            return web.json_response({"error": "account_request_timed_out"}, status=504, headers={"Cache-Control": "no-store"})
        except web.HTTPException:
            raise
        except Exception:
            return web.json_response({"error": "account_service_unavailable"}, status=502, headers={"Cache-Control": "no-store"})

    app.router.add_post("/internal/codex/accounts/{action}", handle)
    async def cleanup(_):
        await manager.close()
    app.on_cleanup.append(cleanup)
    return manager


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run private Codex account control on loopback")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18822)
    args = parser.parse_args()
    # This dedicated development entry point has no external bind option.
    config = json.loads(args.config.read_text())
    app = web.Application(client_max_size=4096)
    install_account_routes(app, config)
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None)
