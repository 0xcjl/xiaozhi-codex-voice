import asyncio
import struct
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from core.providers.realtime.runtime_store import RuntimeStore
from core.providers.realtime.task_announcements import TaskAnnouncements


class TaskDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=RuntimeStore(Path(self.temp.name)/'state')
        self.job=self.store.create_task('scope','call','work')
        self.store.finish_task('scope',self.job['id'],'completed','总额 35')
        self.binding=types.SimpleNamespace(scope='scope',store=self.store,remember=lambda *args:None)
        self.session=types.SimpleNamespace(output_active=False,session_id='voice',output_start=AsyncMock(),output_finish=AsyncMock())
        async def start():self.session.output_active=True
        async def finish():self.session.output_active=False
        self.session.output_start.side_effect=start;self.session.output_finish.side_effect=finish
        self.pipe=types.SimpleNamespace(_output_pcm=AsyncMock(),media=types.SimpleNamespace(session=types.SimpleNamespace(thread_id='thread'),channel=types.SimpleNamespace(readyState='open')))
        self.session.pipe=self.pipe
        self.connection=types.SimpleNamespace(session=self.session,send=AsyncMock(),playback=types.SimpleNamespace(feed=AsyncMock()))
        self.ann=TaskAnnouncements(self.connection,self.binding,timeout=1.5)
    async def asyncTearDown(self):self.ann.close();self.temp.cleanup()
    def dispatch(self,transcript):
        async def request(*args,**kwargs):
            self.ann.event({'type':'session.context.appended','start_ms':1,'end_ms':2})
            self.ann.event({'type':'turn.created','turn':{'id':'report','role':'assistant','start_ms':3}})
            self.ann.event({'type':'turn.done','turn':{'id':'report','role':'assistant','transcript':transcript}})
            await self.ann.feed(struct.pack('<'+'h'*320, *([1000]*320)))
            await self.ann.feed(bytes(25600))
        self.pipe.media.session._request=request
    async def test_correct_identity_and_amount_are_buffered_then_sent(self):
        item=self.store.claim_event('scope',self.ann.claim)
        self.dispatch('任务代号'+self.job['label']+'，总额三十五。')
        await self.ann._announce(item)
        self.pipe._output_pcm.assert_awaited();self.assertIsNone(self.store.claim_event('scope','next'))
    async def test_generation_timeout_is_not_reused_as_playback_deadline(self):
        self.ann.timeout=.05
        item=self.store.claim_event('scope',self.ann.claim)
        self.dispatch('任务代号'+self.job['label']+'，总额三十五。')
        await self.ann._announce(item)
        self.assertIsNone(self.store.claim_event('scope','next'))
    async def test_wrong_amount_never_reaches_device_audio(self):
        item=self.store.claim_event('scope',self.ann.claim)
        self.dispatch('任务代号'+self.job['label']+'，总额五十。')
        with self.assertRaises(ValueError):await self.ann._announce(item)
        self.pipe._output_pcm.assert_not_awaited();self.session.output_start.assert_not_awaited()
    async def test_interrupted_playback_is_reclaimable_by_reconnect(self):
        item=self.store.claim_event('scope',self.ann.claim)
        self.dispatch('任务代号'+self.job['label']+'，总额三十五。')
        self.pipe._output_pcm.side_effect=lambda pcm:self.ann.interrupt()
        await self.ann._announce(item);self.ann.close()
        retried=self.store.claim_event('scope','new-connection')
        self.assertEqual(retried['id'],item['id']);self.assertEqual(retried['attempts'],2)
    async def test_unacknowledged_speech_times_out_without_sending_audio(self):
        self.ann.timeout=.02;self.pipe.media.session._request=AsyncMock()
        item=self.store.claim_event('scope',self.ann.claim)
        with self.assertRaises(TimeoutError):await self.ann._announce(item)
        self.pipe._output_pcm.assert_not_awaited()

    async def test_interruption_reserves_reply_before_replaying_reports(self):
        self.ann.last_sound=self.ann.last_turn=0
        self.ann.interrupt()
        self.assertFalse(self.ann.idle())
        self.ann.event({'type':'turn.done','turn':{'id':'user','role':'user','transcript':'What herb?'}})
        self.ann.last_turn=0
        self.assertFalse(self.ann.idle())
        self.ann.event({'type':'turn.done','turn':{'id':'answer','role':'assistant','transcript':'Rosemary.'}})
        self.ann.last_turn=0
        self.assertTrue(self.ann.idle())

    async def test_verified_audio_replay_uses_cache_and_still_finishes_delivery(self):
        from core.providers.realtime.task_announcements import VerifiedAudioCache
        self.binding.audio_cache=VerifiedAudioCache();self.pipe.media.session.confirmed_model='gpt-6.1-terra'
        item=self.store.claim_event('scope',self.ann.claim)
        self.dispatch('任务'+self.job['label']+'，总额三十五。')
        await self.ann._announce(item)
        self.store.retry('scope',self.job['id']);item=self.store.claim_event('scope',self.ann.claim)
        self.pipe.media.session._request=AsyncMock(side_effect=AssertionError('cached replay must not regenerate speech'))
        await self.ann._announce(item)
        self.pipe.media.session._request.assert_not_awaited()
        self.assertIsNone(self.store.claim_event('scope','next'))
    async def test_wrong_numeric_audio_never_enters_cache(self):
        from core.providers.realtime.task_announcements import VerifiedAudioCache
        cache=VerifiedAudioCache();self.binding.audio_cache=cache;self.pipe.media.session.confirmed_model='gpt-6.1-terra'
        item=self.store.claim_event('scope',self.ann.claim);self.dispatch('任务'+self.job['label']+'，总额35或者50。')
        with self.assertRaises(ValueError):await self.ann._announce(item)
        self.assertEqual(cache.size,0)

    async def test_interrupted_verified_cache_replays_full_report_after_reconnect(self):
        from core.providers.realtime.task_announcements import VerifiedAudioCache
        self.binding.audio_cache=VerifiedAudioCache();self.pipe.media.session.confirmed_model='gpt-6.1-terra'
        item=self.store.claim_event('scope',self.ann.claim);self.dispatch('任务'+self.job['label']+'，总额35。')
        self.pipe._output_pcm.side_effect=lambda pcm:self.ann.interrupt()
        await self.ann._announce(item);self.ann.close()
        replay=TaskAnnouncements(self.connection,self.binding,timeout=1.5)
        try:
            item=self.store.claim_event('scope',replay.claim)
            self.pipe._output_pcm.side_effect=None
            self.pipe.media.session._request=AsyncMock(side_effect=AssertionError('must reuse verified full audio'))
            await replay._announce(item)
            self.pipe.media.session._request.assert_not_awaited()
            self.assertIsNone(self.store.claim_event('scope','next'))
        finally:replay.close()
