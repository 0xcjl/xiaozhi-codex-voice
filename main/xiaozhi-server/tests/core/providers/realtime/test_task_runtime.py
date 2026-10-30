import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from core.providers.realtime.task_runtime import TaskRuntime


class TaskRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.workspace=self.root/'workspace';self.workspace.mkdir();self.profile=self.root/'profile';self.profile.mkdir()
        self.settings={'workspace':str(self.workspace),'device_runtime':{'state_dir':str(self.root/'private'),'task_timeout_seconds':1}}
        self.runtime=TaskRuntime(self.settings,worker=AsyncMock(return_value='verified result 57'))
        self.binding=self.runtime.bind('123','connection','device',self.profile,AsyncMock())
    async def asyncTearDown(self):
        await self.runtime.close();self.temp.cleanup()
    async def wait_done(self):
        async with asyncio.timeout(2):
            while self.runtime.running:await asyncio.sleep(.01)
    async def test_concurrent_tasks_keep_distinct_identity_and_results(self):
        release=asyncio.Event();started=[]
        async def worker(binding,job):started.append(job['id']);await release.wait();return job['prompt']
        self.runtime.worker=worker
        a=await self.runtime.submit(self.binding,'a','result 57');b=await self.runtime.submit(self.binding,'b','result 94')
        await asyncio.sleep(.02);self.assertEqual(len(started),2)
        release.set();await self.wait_done()
        self.assertEqual(self.runtime.store.task(self.binding.scope,a['id'])['result'],'result 57')
        self.assertEqual(self.runtime.store.task(self.binding.scope,b['id'])['result'],'result 94')
    async def test_status_never_starts_work_and_foreign_id_is_denied(self):
        a=await self.runtime.submit(self.binding,'a','calculate')
        status=await self.binding.tool_call({'tool':'xiaozhi_task_status','arguments':{'taskId':a['id']}})
        self.assertIn(status['state'],('queued','running','completed'))
        with self.assertRaises(ValueError):await self.binding.tool_call({'tool':'xiaozhi_task_status','arguments':{'taskId':'foreign'}})
        self.assertEqual(len(self.runtime.store.tasks(self.binding.scope)),1)
    async def test_timeout_cancels_worker_and_preserves_failed_status(self):
        stopped=asyncio.Event()
        async def worker(*args):
            try:await asyncio.Future()
            finally:stopped.set()
        self.runtime.worker=worker;self.runtime.timeout=.02
        a=await self.runtime.submit(self.binding,'a','calculate');await self.wait_done()
        self.assertTrue(stopped.is_set());self.assertEqual(self.runtime.store.task(self.binding.scope,a['id'])['state'],'failed')
    async def test_repeated_tool_call_only_executes_once(self):
        a=await self.runtime.submit(self.binding,'a','calculate');await self.runtime.submit(self.binding,'a','calculate')
        await self.wait_done();self.runtime.worker.assert_awaited_once()
        self.assertEqual(len(self.runtime.store.tasks(self.binding.scope)),1)
    async def test_revoked_access_never_starts_worker(self):
        self.binding.verify.side_effect=PermissionError()
        with self.assertRaises(PermissionError):await self.runtime.submit(self.binding,'a','calculate')
        self.runtime.worker.assert_not_awaited()
    async def test_revocation_during_work_stops_worker_without_success(self):
        stopped=asyncio.Event()
        async def worker(*args):
            try:await asyncio.Future()
            finally:stopped.set()
        self.runtime.worker=worker
        self.binding.verify.side_effect=[None,None,PermissionError('revoked')]
        sleep=asyncio.sleep
        async def short_guard(delay):await sleep(.001 if delay==10 else delay)
        with patch('core.providers.realtime.task_runtime.asyncio.sleep',side_effect=short_guard):
            job=await self.runtime.submit(self.binding,'revoked-in-flight','work')
            await self.wait_done()
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.runtime.store.task(self.binding.scope,job['id'])['state'],'failed')
    async def test_close_marks_queued_tasks_interrupted_and_releases_lock(self):
        async def worker(*args):await asyncio.Future()
        self.runtime.worker=worker
        jobs=[await self.runtime.submit(self.binding,str(i),'work') for i in range(4)]
        await asyncio.sleep(.01);await self.runtime.close();await self.runtime.close()
        self.assertTrue(all(self.runtime.store.task(self.binding.scope,j['id'])['state']=='interrupted' for j in jobs))
        reopened=TaskRuntime(self.settings)
        await reopened.close()
    async def test_explicit_task_cancellation_has_its_own_terminal_state(self):
        async def worker(*args):await asyncio.Future()
        self.runtime.worker=worker
        job=await self.runtime.submit(self.binding,'a','calculate');await asyncio.sleep(.01)
        result=await self.runtime.cancel(self.binding,job['id'])
        self.assertEqual(result['state'],'cancelled')
        with self.assertRaises(ValueError):await self.runtime.cancel(self.binding,'foreign')
    async def test_runtime_state_cannot_be_opened_by_two_process_owners(self):
        with self.assertRaises(ValueError):TaskRuntime(self.settings)
    async def test_scope_isolation_and_case_normalization(self):
        other=self.runtime.bind('124','connection','device',self.profile,AsyncMock())
        upper=self.runtime.bind('123','connection','DEVICE',self.profile,AsyncMock())
        self.assertNotEqual(other.scope,self.binding.scope);self.assertEqual(upper.scope,self.binding.scope)
    async def test_machine_ledger_is_never_inserted_as_actual_user_speech(self):
        await self.runtime.submit(self.binding,'a','calculate')
        self.binding.remember('utterance','user','remember rosemary')
        items=self.binding.initial_items()
        self.assertEqual([i['text'] for i in items if i['role']=='user'],['remember rosemary'])
        self.assertEqual(items[0]['role'],'developer')

    async def test_worker_model_is_unknown_until_confirmed_and_persisted_after_reopen(self):
        job=self.runtime.store.create_task(self.binding.scope,'model-proof','work')
        self.assertEqual(self.runtime.public(job)['execution'],{})
        with self.assertRaises(ValueError):self.runtime.store.record_execution(self.binding.scope,job['id'],'gpt-6.1-terra','thread-worker')
        self.runtime.store.start_task(self.binding.scope,job['id'])
        self.runtime.store.record_execution(self.binding.scope,job['id'],'gpt-6.1-terra','thread-worker')
        self.runtime.store.finish_task(self.binding.scope,job['id'],'completed','done')
        stored=self.runtime.public(self.runtime.store.task(self.binding.scope,job['id']))
        self.assertEqual(stored['execution']['model'],'gpt-6.1-terra')
        other=self.runtime.bind('other','connection','device',self.profile,AsyncMock())
        with self.assertRaises(ValueError):self.runtime.store.record_execution(other.scope,job['id'],'other','thread')
        await self.runtime.close();self.runtime=TaskRuntime(self.settings)
        self.assertEqual(self.runtime.public(self.runtime.store.task(self.binding.scope,job['id']))['execution'],stored['execution'])

    async def test_ordinary_live_lookup_never_creates_background_job(self):
        self.binding.remember('live-query','user','请查一下现在 BTC 的价格')
        result=await self.binding.tool_call({'tool':'xiaozhi_start_task','callId':'bad-route','arguments':{'text':'query current BTC price'}})
        self.assertFalse(result['accepted']);self.assertFalse(self.runtime.store.tasks(self.binding.scope))
        self.runtime.worker.assert_not_awaited()
    async def test_explicit_background_request_still_starts_work(self):
        self.binding.remember('explicit-work','user','请让后台 Codex 计算八十六减二十九')
        result=await self.binding.tool_call({'tool':'xiaozhi_start_task','callId':'real-job','arguments':{'text':'calculate 86 minus 29'}})
        await self.wait_done();self.assertEqual(self.runtime.store.task(self.binding.scope,result['id'])['state'],'completed')
