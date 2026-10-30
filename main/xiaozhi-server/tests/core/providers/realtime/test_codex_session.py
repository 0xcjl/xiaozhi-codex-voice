import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from core.providers.realtime.codex_session import CodexRealtimeSession, CodexSessionError


class FakeProcess:
    def __init__(self, account="chatgpt", model="gpt-6.1-terra", realtime_error=None):
        self.account = account
        self.model = model
        self.realtime_error = realtime_error
        self.stdout = asyncio.StreamReader()
        self.stdin = self
        self.returncode = None
        self.requests = []
        self.terminated = asyncio.Event()

    def write(self, data):
        message = json.loads(data)
        self.requests.append(message)
        if "id" not in message:
            return
        method = message["method"]
        result = {}
        if method == "account/read":
            result = {"account": {"type": self.account}}
        elif method == "thread/start":
            result = {"model": self.model, "thread": {"id": "thread-1"}}
        elif method == "thread/realtime/start":
            event = {"method": self.realtime_error, "params": {}} if self.realtime_error else {"method": "thread/realtime/sdp", "params": {"sdp": "answer"}}
            self.stdout.feed_data((json.dumps(event) + "\n").encode())
        self.stdout.feed_data((json.dumps({"id": message["id"], "result": result}) + "\n").encode())

    async def drain(self):
        await asyncio.sleep(0)

    def terminate(self):
        self.returncode = 0
        self.stdout.feed_eof()
        self.terminated.set()

    def kill(self):
        self.terminate()

    async def wait(self):
        await self.terminated.wait()
        return self.returncode


class CodexSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_error_does_not_wait_for_sdp_timeout(self):
        for method in ("thread/realtime/error", "thread/realtime/closed"):
            with self.subTest(method=method):
                process = FakeProcess(realtime_error=method)
                with patch("asyncio.create_subprocess_exec", return_value=process):
                    session = CodexRealtimeSession(Path.cwd())
                    with self.assertRaisesRegex(CodexSessionError, "startup"):
                        await asyncio.wait_for(session.start("offer"), 0.3)
                self.assertTrue(process.terminated.is_set())

    async def test_uses_terra_and_closes_only_owned_process(self):
        process = FakeProcess()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            session = CodexRealtimeSession(Path.cwd())
            self.assertEqual(await session.start("offer"), "answer")
            await session.close()
        thread = next(m for m in process.requests if m.get("method") == "thread/start")
        self.assertEqual(thread["params"]["model"], "gpt-6.1-terra")
        self.assertTrue(thread["params"]["ephemeral"])
        self.assertEqual(thread["params"]["sandbox"], "read-only")
        self.assertTrue(process.terminated.is_set())

    async def test_rejects_non_chatgpt_account_and_closes(self):
        process = FakeProcess(account="apikey")
        with patch("asyncio.create_subprocess_exec", return_value=process):
            session = CodexRealtimeSession(Path.cwd())
            with self.assertRaisesRegex(CodexSessionError, "ChatGPT login"):
                await session.start("offer")
        self.assertTrue(process.terminated.is_set())
        self.assertFalse(any(m.get("method") == "thread/start" for m in process.requests))

    async def test_model_mismatch_stops_session(self):
        process = FakeProcess(model="other")
        with patch("asyncio.create_subprocess_exec", return_value=process):
            session = CodexRealtimeSession(Path.cwd())
            with self.assertRaisesRegex(CodexSessionError, "model"):
                await session.start("offer")
        self.assertTrue(process.terminated.is_set())

    async def test_forwards_background_task_events_for_future_receipts(self):
        process = FakeProcess()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            session = CodexRealtimeSession(Path.cwd())
            await session.start("offer")
            event = {"method": "turn/completed", "params": {"turn": {"id": "turn-1"}}}
            process.stdout.feed_data((json.dumps(event) + "\n").encode())
            received = await asyncio.wait_for(session.events.get(), 1)
            self.assertEqual(received, event)
            await session.close()
    async def test_persistent_resume_never_silently_creates_a_new_thread(self):
        from types import SimpleNamespace
        from unittest.mock import Mock,AsyncMock
        process=FakeProcess()
        binding=SimpleNamespace(thread=lambda:'thread-saved',save_thread=Mock(),initial_items=lambda:[{'role':'user','text':'remember cedar'}])
        with patch('asyncio.create_subprocess_exec',return_value=process):
            session=CodexRealtimeSession(Path.cwd(),conversation=binding)
            async def request(method,params,timeout=30):
                if method=='account/read':return {'account':{'type':'chatgpt'}}
                if method=='thread/resume':return {'model':'gpt-6.1-terra','thread':{'id':'different'}}
                return {}
            session._request=AsyncMock(side_effect=request)
            with self.assertRaisesRegex(CodexSessionError,'different thread'):await session.start('offer')
            self.assertFalse(any(c.args[0]=='thread/start' for c in session._request.call_args_list))

    async def test_resumed_thread_mcp_inventory_is_checked_before_realtime_starts(self):
        from types import SimpleNamespace
        from unittest.mock import Mock,AsyncMock
        binding=SimpleNamespace(thread=lambda:'thread-saved',save_thread=Mock(),initial_items=lambda:[])
        session=CodexRealtimeSession(Path.cwd(),conversation=binding,device_tool_server={'enabled':True})
        session.open=AsyncMock();session.close=AsyncMock();calls=[]
        async def request(method,params,timeout=30):
            calls.append((method,params))
            if method=='account/read':return {'account':{'type':'chatgpt'}}
            if method=='mcpServerStatus/list':return {'data':[{'name':'xiaozhi_device','toolsError':None,'tools':{'model_info':{}}}]}
            if method=='thread/resume':return {'model':'gpt-6.1-terra','thread':{'id':'thread-saved'}}
            if method=='thread/realtime/start':session.answer.set_result('answer');return {}
            return {}
        session._request=AsyncMock(side_effect=request)
        self.assertEqual(await session.start('offer'),'answer')
        scoped=next(i for i,(method,params) in enumerate(calls) if method=='mcpServerStatus/list' and params.get('threadId')=='thread-saved')
        realtime=next(i for i,(method,params) in enumerate(calls) if method=='thread/realtime/start')
        self.assertLess(scoped,realtime);self.assertEqual(session.confirmed_model,'gpt-6.1-terra')
        self.assertFalse(any(method=='thread/start' for method,_ in calls))
        initial=calls[realtime][1]['initialItems']
        self.assertIn('gpt-6.1-terra',initial[-1]['text'])

    async def test_device_mcp_approval_is_scoped_to_exact_allowed_tools(self):
        process=FakeProcess()
        config={'command':'python','args':['test-only-adapter.py'],'env':{'XIAOZHI_TOOL_PORT':'1234','XIAOZHI_TOOL_SECRET':'test-only'}}
        with patch('asyncio.create_subprocess_exec',return_value=process) as launch:
            session=CodexRealtimeSession(Path.cwd(),device_tool_server=config)
            await session.open();await session.close()
        args=launch.call_args.args
        self.assertIn('mcp_servers.xiaozhi_device.tools.audio_speaker_set_volume.approval_mode="approve"',args)
        self.assertFalse(any('upgrade_firmware' in arg or 'default_tools_approval_mode' in arg for arg in args))

    async def test_event_overflow_terminates_owned_process(self):
        process = FakeProcess()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            session = CodexRealtimeSession(Path.cwd())
            await session.start("offer")
            event = (json.dumps({"method": "thread/realtime/started", "params": {}}) + "\n").encode()
            process.stdout.feed_data(event * 129)
            await asyncio.wait_for(process.terminated.wait(), 1)
            self.assertTrue(session.event_overflow)
            await session.close()


if __name__ == "__main__":
    unittest.main()
