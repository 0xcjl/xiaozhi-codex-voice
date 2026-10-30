import types
import unittest
from unittest.mock import AsyncMock
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from core.providers.realtime.account_control import install_account_routes

class AccountControlHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = types.SimpleNamespace(status=AsyncMock(return_value={"state":"signed_out"}),
            start=AsyncMock(return_value={"state":"pending","verificationUrl":"https://auth.openai.com/codex/device","userCode":"ABCD-1234"}),
            cancel=AsyncMock(return_value={"state":"cancelled"}), close=AsyncMock())
        self.manager.refresh=AsyncMock(return_value={"state":"signed_in"})
        self.manager.reauthorize=AsyncMock(return_value={"state":"pending"})
        self.config = {"manager-api":{"secret":"synthetic-secret-longer-than-24"}, "codex_realtime":{
            "account_control":{"enabled":True},"account_profiles_dir":"/unused/test-profiles", "workspace":"/unused/workspace", "allowed_user_ids":[123]}}
        app = web.Application(client_max_size=4096); install_account_routes(app,self.config,manager=self.manager)
        self.client = TestClient(TestServer(app)); await self.client.start_server()
        self.body={"ownerId":"123","connectionId":"c6f21b51-ff26-48a9-87ac-83754852a6dd"}
        self.headers={"Authorization":"Bearer synthetic-secret-longer-than-24"}
    async def asyncTearDown(self):
        await self.client.close(); self.manager.close.assert_awaited_once()
    async def test_secret_required_before_account_methods(self):
        response=await self.client.post('/internal/codex/accounts/start',json=self.body)
        self.assertEqual(response.status,401); self.manager.start.assert_not_awaited()
    async def test_owner_requires_host_grant(self):
        response=await self.client.post('/internal/codex/accounts/start',json={**self.body,"ownerId":"999"},headers=self.headers)
        self.assertEqual(response.status,403); self.manager.start.assert_not_awaited()
    async def test_actual_http_start_status_cancel_are_noncacheable(self):
        for action in ['start','status','cancel','refresh','reauthorize']:
            response=await self.client.post('/internal/codex/accounts/'+action,json=self.body,headers=self.headers)
            self.assertEqual(response.status,200); self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertIn('state',await response.json())
    async def test_arbitrary_rpc_and_directories_are_rejected(self):
        response=await self.client.post('/internal/codex/accounts/start',json={**self.body,"codexHome":"/daily"},headers=self.headers)
        self.assertEqual(response.status,400)
        response=await self.client.post('/internal/codex/accounts/logout',json=self.body,headers=self.headers)
        self.assertEqual(response.status,404)
    async def test_refresh_requires_same_host_grant_and_never_accepts_browser_token(self):
        response=await self.client.post('/internal/codex/accounts/refresh',json=self.body)
        self.assertEqual(response.status,401)
        response=await self.client.post('/internal/codex/accounts/refresh',json={**self.body,'ownerId':'999'},headers=self.headers)
        self.assertEqual(response.status,403);self.manager.refresh.assert_not_awaited()
    async def test_chunked_body_is_bounded(self):
        async def chunks():
            yield b' ' * 4096
            yield b' ' * 4096
        response=await self.client.post('/internal/codex/accounts/start',data=chunks(),headers=self.headers)
        self.assertEqual(response.status,413); self.manager.start.assert_not_awaited()
    async def test_upstream_exception_body_is_never_returned(self):
        self.manager.start.side_effect=RuntimeError('TOKEN=NEVER-RETURN')
        response=await self.client.post('/internal/codex/accounts/start',json=self.body,headers=self.headers)
        self.assertEqual(response.status,502); self.assertNotIn('NEVER',await response.text())
