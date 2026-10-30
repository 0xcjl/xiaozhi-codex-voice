import types
import unittest

from core.auth import AuthManager
from core.providers.realtime.service import RealtimeConnection, try_handle_realtime


class ServiceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_input_buffer_rejects_overflow(self):
        class Socket:
            async def __aiter__(self):
                for _ in range(200):
                    yield bytes(17)
        connection = RealtimeConnection(Socket(), {"workspace": "/tmp"})
        connection.frame_ms = 60
        with self.assertRaisesRegex(BufferError, "buffer full"):
            await connection._receive()
        self.assertEqual(connection.input_queue.qsize(), 166)
        connection._discard_input()
        self.assertEqual(connection.pending_bytes, 0)

    def setUp(self):
        self.auth = AuthManager("synthetic-test-secret")
        self.config = {"server": {"auth": {"enabled": True}},
                       "codex_realtime": {"enabled": True, "device_ids": ["test-device"], "workspace": "/tmp"}}
        self.calls = []

    def socket(self, token=None):
        headers = {"device-id": "test-device", "client-id": "test-client"}
        if token is not None:
            headers["authorization"] = "Bearer " + token
        calls = self.calls
        class Socket:
            request = types.SimpleNamespace(headers=headers, path="/xiaozhi/v1/")
            async def close(self, **kwargs):
                calls.append(kwargs)
        return Socket()

    async def test_disabled_does_not_touch_regular_connections(self):
        self.config["codex_realtime"]["enabled"] = False
        self.assertFalse(await try_handle_realtime(self.socket(), self.config, self.auth))
        self.assertEqual(self.calls, [])

    async def test_selected_device_cannot_use_auth_disabled_or_whitelist_bypass(self):
        self.config["server"]["auth"]["allowed_devices"] = ["test-device"]
        self.assertTrue(await try_handle_realtime(self.socket(), self.config, self.auth))
        self.assertEqual(self.calls[-1]["code"], 1008)
        self.config["server"]["auth"]["enabled"] = False
        token = self.auth.generate_token("test-client", "test-device")
        self.assertTrue(await try_handle_realtime(self.socket(token), self.config, self.auth))
        self.assertEqual(self.calls[-1]["code"], 1008)

    async def test_authenticated_device_runs_handler_and_releases_slot(self):
        calls = self.calls
        class Connection:
            def __init__(self, websocket, config):
                pass
            async def run(self):
                calls.append("run")
        active = set()
        token = self.auth.generate_token("test-client", "test-device")
        self.assertTrue(await try_handle_realtime(self.socket(token), self.config, self.auth,
                                                 active_devices=active, connection_factory=Connection))
        self.assertEqual(calls, ["run"])
        self.assertEqual(active, set())

    async def test_other_device_token_and_duplicate_connection_rejected(self):
        wrong = self.auth.generate_token("other-client", "test-device")
        self.assertTrue(await try_handle_realtime(self.socket(wrong), self.config, self.auth))
        self.assertEqual(self.calls[-1]["code"], 1008)
        token = self.auth.generate_token("test-client", "test-device")
        active = {"test-device"}
        self.assertTrue(await try_handle_realtime(self.socket(token), self.config, self.auth, active_devices=active))
        self.assertEqual(active, {"test-device"})

    async def test_runtime_failure_closes_socket_and_releases_slot_without_logging_content(self):
        class Connection:
            def __init__(self, *_):
                pass
            async def run(self):
                raise RuntimeError("private provider content")
        token = self.auth.generate_token("test-client", "test-device")
        active = set()
        with self.assertLogs("core.providers.realtime.service", level="WARNING") as logs:
            self.assertTrue(await try_handle_realtime(self.socket(token), self.config, self.auth,
                                                     active_devices=active, connection_factory=Connection))
        self.assertEqual(active, set())
        self.assertEqual(self.calls[-1]["code"], 1011)
        self.assertNotIn("private provider content", " ".join(logs.output))



class DeviceMcpReceiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_mcp_is_routed_during_startup_without_entering_audio_queue(self):
        from unittest.mock import Mock
        import json
        class Socket:
            async def __aiter__(self):
                yield json.dumps({'type':'mcp','payload':{'id':1,'result':{'padding':'x'*5000}}})
        connection=RealtimeConnection(Socket(),{'workspace':'/tmp'})
        connection.device_mcp=Mock();await connection._receive()
        connection.device_mcp.receive.assert_called_once()
        self.assertEqual(connection.input_queue.qsize(),0)
    async def test_model_query_reads_confirmed_model_without_starting_or_replaying_tasks(self):
        from unittest.mock import AsyncMock,Mock
        connection=RealtimeConnection(Mock(),{'workspace':'/tmp'})
        connection.conversation=Mock();connection.conversation.verify=AsyncMock()
        connection.conversation.store.tasks.return_value=[]
        connection.device_mcp=Mock(tools={},discovery_error=None)
        connection.session=Mock();connection.session.pipe.media.session.confirmed_model='gpt-6.1-terra'
        connection.session.pipe.media.session.thread_id='test-thread'
        result=await connection.device_tool_request('tools/call',{'name':'model_info'})
        import json
        value=json.loads(result['content'][0]['text'])
        self.assertEqual(value['backingModel'],'gpt-6.1-terra');self.assertIsNone(value['realtimeAcousticModel'])
        connection.conversation.runtime.submit.assert_not_called()
        connection.conversation.runtime.queue_status.assert_not_called()

    def test_premature_device_success_requires_current_turn_tool_ack(self):
        from unittest.mock import Mock
        connection=RealtimeConnection(Mock(),{'workspace':'/tmp'});connection.playback=Mock()
        connection.begin_user_turn('把音量调到百分之九十五')
        self.assertTrue(connection.device_control_pending)
        self.assertNotIn('已调到95',connection.device_answer('turn-without-ack','已调到95'))
        connection.verified_device_turns.add('verified-turn');connection.device_summaries['verified-turn']='设备读回音量为46%，与请求不一致，操作未确认成功。'
        self.assertEqual(connection.device_answer('verified-turn','已调到95'),connection.device_summaries['verified-turn'])
        connection.begin_user_turn('我明天打算做什么？')
        self.assertFalse(connection.device_control_pending)

    def test_concurrent_device_summaries_never_borrow_another_turn_volume(self):
        from unittest.mock import Mock
        import json
        connection=RealtimeConnection(Mock(),{'workspace':'/tmp'});connection.device_control_pending=True
        def item(volume):return {'tool':'audio_speaker_set_volume','result':{'content':[{'type':'text','text':json.dumps({'measuredVolume':volume,'matchesRequested':True})}]}}
        connection.remember_device_result('turn-b',item(68));connection.remember_device_result('turn-a',item(46))
        self.assertIn('68%',connection.device_answer('turn-b','wrong'))
        self.assertIn('46%',connection.device_answer('turn-a','wrong'))

    def test_verified_volume_reply_is_short_without_losing_measured_value(self):
        from unittest.mock import Mock
        import json
        connection=RealtimeConnection(Mock(),{'workspace':'/tmp'});connection.device_control_pending=True
        connection.remember_device_result('turn',{'tool':'audio_speaker_set_volume','result':{'content':[{'type':'text','text':json.dumps({'measuredVolume':68,'matchesRequested':True})}]}})
        self.assertEqual(connection.device_answer('turn','anything'),'音量68%。')

    def test_early_volume_confirmation_requires_matching_current_readback(self):
        from unittest.mock import Mock
        import json
        connection=RealtimeConnection(Mock(),{'workspace':'/tmp'});connection.playback=Mock();connection.announcements=Mock()
        connection.begin_user_turn('把音量调到百分之六十八')
        def result(value,matched=True):return {'tool':'audio_speaker_set_volume','arguments':{'volume':value},'result':{'content':[{'type':'text','text':json.dumps({'measuredVolume':value,'matchesRequested':matched})}]}}
        connection.handle_device_result('old',result(46));connection.announcements.reply.assert_not_called()
        connection.handle_device_result('unconfirmed',result(68,False));connection.announcements.reply.assert_not_called()
        connection.handle_device_result('current',result(68));connection.announcements.reply.assert_called_once_with('current','音量68%。')
        connection.handle_device_result('current',result(68));connection.announcements.reply.assert_called_once()
        self.assertIn('current',connection.device_early_turns)
    def test_volume_fast_confirmation_rejects_mixed_or_ambiguous_speech(self):
        from core.providers.realtime.service import absolute_volume
        self.assertEqual(absolute_volume('请把音量设为百分之六十二。'),62)
        self.assertEqual(absolute_volume('Use the device tool to set the speaker volume to forty six percent. Report the actual volume after reading it back.'),46)
        self.assertEqual(absolute_volume('Use the device tool to set the speaker volume to forty-six percent ;report the actual volume after reading it back'),46)
        for text in ('不要把音量调到六十八','音量六十八是什么意思','把音量调大一点','把音量调到六十八，再启动后台任务','把音量调到百分之一百零一'):
            self.assertIsNone(absolute_volume(text))

    async def test_early_verified_reply_is_not_queued_again_at_backing_completion(self):
        import asyncio
        from unittest.mock import AsyncMock, Mock
        connection=RealtimeConnection(Mock(),{'workspace':'/tmp'})
        connection.announcements=Mock();connection.announcements.event.return_value=False
        sdk=Mock(thread_id='thread',tool_failures=set(),control_turns=set())
        connection.session=Mock();connection.session.pipe.media.session=sdk
        connection.device_early_turns.add('turn');connection.backing_answers['turn']='音量68%。'
        queue=asyncio.Queue()
        await queue.put({'method':'turn/completed','params':{'threadId':'thread','turn':{'id':'turn','status':'completed'}}})
        task=asyncio.create_task(connection._events(queue,app_server=True))
        await asyncio.sleep(0)
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        connection.announcements.reply.assert_not_called()
        self.assertNotIn('turn',connection.backing_answers)

    def test_verified_model_query_is_spoken_even_when_task_status_has_its_own_report(self):
        import json
        from unittest.mock import Mock
        connection=RealtimeConnection(Mock(),{'workspace':'/tmp'})
        connection.playback=Mock();connection.announcements=Mock()
        connection.begin_user_turn('What is my code word, and which model executed the background task?')
        result={'backingModel':'gpt-6.1-terra','realtimeAcousticModel':None,
                'tasks':[{'execution':{'model':'gpt-6.1-terra'}}]}
        connection.handle_device_result('model-turn',{'tool':'model_info','result':{'content':[{'type':'text','text':json.dumps(result)}]}})
        connection.announcements.reply.assert_called_once()
        spoken=connection.announcements.reply.call_args.args[1]
        self.assertIn('gpt-6.1-terra',spoken)
        self.assertIn('任务执行模型',spoken)
        self.assertIn('未公开',spoken)
        self.assertIn('model-turn',connection.device_early_turns)
        connection.begin_user_turn('把音量调到六十八')
        connection.handle_device_result('unsolicited-model',{'tool':'model_info','result':{'content':[{'type':'text','text':json.dumps(result)}]}})
        connection.announcements.reply.assert_called_once()
