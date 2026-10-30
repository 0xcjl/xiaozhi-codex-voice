import types
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock
from core.providers.realtime.service import try_handle_realtime


class ConsoleSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setup_route(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()/"profiles"
        profile = root/"12"/"c6f21b51-ff26-48a9-87ac-83754852a6dd"
        profile.mkdir(parents=True, mode=0o700)
        (profile/"auth.json").write_text("synthetic fixture, no credentials")
        self.socket = types.SimpleNamespace(request=types.SimpleNamespace(headers={
            "device-id": "device", "client-id": "client", "authorization": "Bearer native1.synthetic"}), close=AsyncMock())
        self.auth = types.SimpleNamespace(verify_token=lambda *a, **k: True, verify_native_token=lambda token,*a: token.startswith("native1."))
        self.config = {"server": {"auth": {"enabled": True}}, "read_config_from_api": True,
            "codex_realtime": {"enabled": True, "console_selection": True, "allowed_user_ids": [12], "account_profiles_dir": str(root), "workspace": str(Path.cwd())}}
        self.lookup = AsyncMock(return_value={"kind": "chatgpt_realtime", "enabled": True,
            "runtimeId": "local", "model": "gpt-6.1-terra", "userId": 12, "id": "c6f21b51-ff26-48a9-87ac-83754852a6dd"})
        self.connection = types.SimpleNamespace(run=AsyncMock())
        self.factory = lambda *a: self.connection

    async def route(self):
        return await try_handle_realtime(self.socket, self.config, self.auth,
            connection_factory=self.factory, connection_lookup=self.lookup)

    async def test_selected_native_uses_host_grant(self):
        self.setup_route(); self.assertTrue(await self.route()); self.connection.run.assert_awaited_once()
    async def test_device_case_cannot_bypass_single_active_connection(self):
        self.setup_route();self.socket.request.headers['device-id']='DEVICE'
        await try_handle_realtime(self.socket,self.config,self.auth,active_devices={'device'},connection_factory=self.factory,connection_lookup=self.lookup)
        self.connection.run.assert_not_awaited()
        self.assertEqual(self.socket.close.await_args.kwargs['code'],1008)
    async def test_persistent_device_credential_requires_trusted_verification(self):
        self.setup_route(); self.socket.request.headers["authorization"]="Bearer native2.synthetic"
        verifier=AsyncMock(return_value={"valid":True,"connectionId":self.lookup.return_value["id"],"userId":12})
        await try_handle_realtime(self.socket,self.config,self.auth,connection_factory=self.factory,connection_lookup=self.lookup,credential_verifier=verifier)
        self.connection.run.assert_awaited_once();verifier.assert_awaited_once()
    async def test_persistent_credential_for_other_connection_cannot_start_voice(self):
        self.setup_route(); self.socket.request.headers["authorization"]="Bearer native2.synthetic"
        verifier=AsyncMock(return_value={"valid":True,"connectionId":"other","userId":12})
        await try_handle_realtime(self.socket,self.config,self.auth,connection_factory=self.factory,connection_lookup=self.lookup,credential_verifier=verifier)
        self.connection.run.assert_not_awaited();self.assertEqual(self.socket.close.await_args.kwargs["code"],1013)
    async def test_java_long_user_ids_serialized_as_strings(self):
        self.setup_route(); self.lookup.return_value["userId"] = "12"
        self.assertTrue(await self.route()); self.connection.run.assert_awaited_once()
    async def test_missing_dedicated_login_never_falls_back_to_daily_account(self):
        self.setup_route()
        Path(self.config["codex_realtime"]["account_profiles_dir"], "12", self.lookup.return_value["id"], "auth.json").unlink()
        self.assertTrue(await self.route())
        self.assertEqual(self.socket.close.await_args.kwargs["code"], 1008)
        self.connection.run.assert_not_awaited()
    async def test_generic_ota_token_is_rejected_even_after_device_switches_to_subscription(self):
        self.setup_route(); self.socket.request.headers["authorization"]="Bearer generic"
        self.assertTrue(await self.route()); self.connection.run.assert_not_awaited()
        self.assertEqual(self.socket.close.await_args.kwargs["code"],1008)
    async def test_no_binding_and_api_use_ordinary_route(self):
        self.setup_route()
        for value in [None, {"kind": "api"}]:
            self.lookup.return_value = value; self.socket.request.headers["authorization"]="Bearer generic"; self.assertFalse(await self.route())
        self.connection.run.assert_not_awaited()
    async def test_admin_cannot_implicitly_use_host_subscription(self):
        self.setup_route(); self.lookup.return_value["userId"] = 99
        self.assertTrue(await self.route()); self.socket.close.assert_awaited_once_with(code=1008, reason="Realtime runtime access is not granted")
        self.connection.run.assert_not_awaited()
    async def test_changed_runtime_or_model_fails_closed(self):
        for field, value in [("runtimeId", "cloud"), ("model", "other"), ("enabled", False), ("kind", "unknown")]:
            self.setup_route(); self.lookup.return_value[field] = value
            self.assertTrue(await self.route()); self.assertEqual(self.socket.close.await_args.kwargs["code"], 1008)
    async def test_lookup_timeout_is_not_default_fallback(self):
        self.setup_route(); self.lookup.side_effect = TimeoutError()
        self.assertTrue(await self.route()); self.assertEqual(self.socket.close.await_args.kwargs["code"], 1013)
        self.connection.run.assert_not_awaited()
    async def test_bad_token_never_reaches_console(self):
        self.setup_route(); self.socket.request.headers["authorization"]="Bearer invalid"; self.auth.verify_token = lambda *a, **k: False
        self.assertTrue(await self.route()); self.lookup.assert_not_awaited()
    async def test_disabled_feature_leaves_existing_route_unchanged(self):
        self.setup_route(); self.config["codex_realtime"]["enabled"] = False
        self.assertFalse(await self.route()); self.lookup.assert_not_awaited()
