import asyncio
import types
import unittest
from pathlib import Path
from unittest.mock import Mock
from core.providers.realtime.service import RealtimeConnection


class BackingReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_task_handover_is_spoken_but_task_control_answer_is_not_duplicated(self):
        socket=types.SimpleNamespace(request=types.SimpleNamespace(headers={}))
        connection=RealtimeConnection(socket,{'workspace':str(Path.cwd())})
        sdk=types.SimpleNamespace(thread_id='thread',control_turns={'controlled'},tool_failures=set())
        connection.session=types.SimpleNamespace(pipe=types.SimpleNamespace(media=types.SimpleNamespace(session=sdk)))
        connection.announcements=types.SimpleNamespace(reply=Mock(),answer_ready=Mock())
        queue=asyncio.Queue()
        for ident,text in [('normal','The answer is 49.'),('controlled','Task launched.')]:
            queue.put_nowait({'method':'item/completed','params':{'threadId':'thread','turnId':ident,'item':{'type':'agentMessage','phase':'final_answer','text':text}}})
            queue.put_nowait({'method':'turn/completed','params':{'threadId':'thread','turn':{'id':ident,'status':'completed'}}})
        task=asyncio.create_task(connection._events(queue,app_server=True))
        try:
            await asyncio.sleep(.01)
            connection.announcements.reply.assert_called_once_with('normal','The answer is 49.')
            self.assertEqual(connection.announcements.answer_ready.call_count,2)
            self.assertFalse(sdk.control_turns);self.assertEqual(connection.backing_answers,{})
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
