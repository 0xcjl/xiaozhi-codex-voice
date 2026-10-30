import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from core.providers.realtime.account_profiles import AccountProfiles, AccountProfileError
from core.providers.realtime.codex_session import CodexRealtimeSession
from test_codex_session import FakeProcess

OWNER = "123"
CONNECTION = "c6f21b51-ff26-48a9-87ac-83754852a6dd"

class AccountProfilesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "profiles"
    def tearDown(self):
        self.temp.cleanup()
    async def test_fresh_status_never_starts_codex_or_reads_daily_login(self):
        manager = AccountProfiles(self.root, Path.cwd(), factory=AsyncMock())
        self.assertEqual((await manager.status(OWNER, CONNECTION))["state"], "signed_out")
        manager.factory.assert_not_called(); self.assertFalse(self.root.exists())
    async def test_path_traversal_and_daily_home_are_rejected(self):
        manager = AccountProfiles(self.root, Path.cwd())
        for owner, connection in [("../1", CONNECTION), (OWNER, "../../.codex"), ("0", CONNECTION)]:
            with self.assertRaises(AccountProfileError): manager.profile(owner, connection)
        with self.assertRaises(AccountProfileError): AccountProfiles(Path.home()/".codex", Path.cwd())
    async def test_symlink_profile_cannot_target_another_login(self):
        manager = AccountProfiles(self.root, Path.cwd())
        self.root.mkdir(mode=0o700); (self.root/OWNER).mkdir(mode=0o700)
        (self.root/OWNER/CONNECTION).symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(AccountProfileError): manager.profile(OWNER, CONNECTION, create=True)
    async def test_profiles_cannot_be_created_inside_or_above_agent_workspace(self):
        workspace = Path(self.temp.name)/"workspace"
        with self.assertRaises(AccountProfileError): AccountProfiles(workspace/"profiles", workspace)
        with self.assertRaises(AccountProfileError): AccountProfiles(Path(self.temp.name), workspace)
    async def test_isolated_process_overrides_home_and_forces_file_credentials(self):
        profile = self.root/OWNER/CONNECTION; profile.mkdir(parents=True)
        process = FakeProcess()
        with patch.dict(os.environ, {"CODEX_HOME": "/daily/codex", "OPENAI_API_KEY": "must-not-leak"}):
            with patch("asyncio.create_subprocess_exec", return_value=process) as spawn:
                session = CodexRealtimeSession(Path.cwd(), codex_home=profile)
                await session.open(); await session.close()
        args, kwargs = spawn.call_args
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(profile.resolve()))
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertIn('cli_auth_credentials_store="file"', args)
        self.assertFalse(any(m.get("method") == "thread/start" for m in process.requests))

class FakeAccount:
    def __init__(self):
        self.events = asyncio.Queue(); self.closed = False; self.process = type("P", (), {"returncode": None})()
        self.signed_in = False; self.calls = []
    async def open(self): pass
    async def _request(self, method, params, timeout=30):
        self.calls.append((method, params))
        if method == "account/login/start": return {"type":"chatgptDeviceCode","loginId":"attempt", "verificationUrl":"https://auth.openai.com/codex/device", "userCode":"ABCD-1234"}
        if method == "account/read": return {"account":{"type":"chatgpt", "email":"test@example.invalid", "planType":"plus", "secret":"NEVER"} if self.signed_in else None}
        return {}
    async def close(self): self.closed = True

class AccountLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.account = FakeAccount()
        self.manager = AccountProfiles(Path(self.temp.name)/"profiles", Path.cwd(), factory=lambda *a, **k:self.account, timeout=0.15)
    async def asyncTearDown(self):
        await self.manager.close(); self.temp.cleanup()
    async def test_refresh_uses_only_isolated_profile_and_returns_no_tokens(self):
        self.manager.profile(OWNER,CONNECTION,create=True);self.account.signed_in=True
        result=await self.manager.refresh(OWNER,CONNECTION)
        self.assertEqual(result['state'],'signed_in')
        self.assertIn(('account/read',{'refreshToken':True}),self.account.calls)
        self.assertNotIn('secret',result);self.assertTrue(self.account.closed)
    async def test_refresh_does_not_create_a_login_for_an_unknown_profile(self):
        result=await self.manager.refresh(OWNER,CONNECTION)
        self.assertEqual(result['state'],'signed_out');self.assertEqual(self.account.calls,[])
    async def test_reauthorization_does_not_log_out_or_reuse_stale_cached_identity(self):
        self.manager.profile(OWNER,CONNECTION,create=True);self.account.signed_in=True
        result=await self.manager.reauthorize(OWNER,CONNECTION)
        self.assertEqual(result['state'],'pending')
        self.assertIn(('account/login/start',{'type':'chatgptDeviceCode'}),self.account.calls)
        self.assertFalse(any(method=='account/logout' for method,_ in self.account.calls))
    async def test_repeated_start_reuses_attempt_and_cancel_cleans_up(self):
        first = await self.manager.start(OWNER, CONNECTION)
        self.assertEqual(first["userCode"], "ABCD-1234")
        self.assertEqual(await self.manager.start(OWNER, CONNECTION), first)
        await self.manager.cancel(OWNER, CONNECTION)
        self.assertTrue(self.account.closed)
        self.assertEqual(len([c for c in self.account.calls if c[0]=="account/login/start"]), 1)
        self.assertNotIn("userCode", await self.manager.status(OWNER, CONNECTION))
    async def test_success_reads_account_and_never_exposes_token(self):
        await self.manager.start(OWNER, CONNECTION); self.account.signed_in = True
        await self.account.events.put({"method":"account/login/completed","params":{"loginId":"attempt","success":True}})
        await asyncio.sleep(0.02)
        status = await self.manager.status(OWNER, CONNECTION)
        self.assertEqual(status, {"state":"signed_in","email":"test@example.invalid","planType":"plus"})
        self.assertTrue(self.account.closed)
    async def test_cancel_reports_signin_if_official_consent_already_completed(self):
        await self.manager.start(OWNER, CONNECTION)
        await asyncio.sleep(0)
        self.account.signed_in = True
        result = await self.manager.cancel(OWNER, CONNECTION)
        self.assertEqual(result["state"], "signed_in")
        self.assertTrue(self.account.closed)
    async def test_timeout_clears_code_and_releases_process(self):
        await self.manager.start(OWNER, CONNECTION); await asyncio.sleep(0.2)
        self.assertEqual((await self.manager.status(OWNER, CONNECTION))["state"], "expired")
        self.assertTrue(self.account.closed)
    async def test_untrusted_verification_url_is_rejected(self):
        original = self.account._request
        async def request(method, params, timeout=30):
            result = await original(method, params, timeout)
            if method=="account/login/start": result["verificationUrl"]="https://evil.invalid"
            return result
        self.account._request = request
        with self.assertRaises(AccountProfileError): await self.manager.start(OWNER, CONNECTION)
        self.assertTrue(self.account.closed)
    async def test_wrong_login_notification_cannot_complete_attempt(self):
        await self.manager.start(OWNER, CONNECTION)
        await self.account.events.put({"method":"account/login/completed","params":{"loginId":"other","success":True}})
        await asyncio.sleep(0.02)
        self.assertEqual((await self.manager.status(OWNER, CONNECTION))["state"], "pending")
