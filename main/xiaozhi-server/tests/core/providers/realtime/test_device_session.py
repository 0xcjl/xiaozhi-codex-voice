import asyncio
import unittest

from core.providers.realtime.device_session import DeviceSession
from core.providers.realtime.xiaozhi_protocol import AudioPacket, pack_audio


class FakePipe:
    def __init__(self, workspace, send_opus, **kwargs):
        self.send_opus = send_opus
        self.received = []
        self.interrupted = False
        self.finished = False

    async def start(self):
        pass

    def feed_device_opus(self, data):
        self.received.append(data)

    def begin_output(self):
        self.interrupted = False

    def interrupt_output(self):
        self.interrupted = True

    async def finish_output(self):
        self.finished = True

    async def close(self):
        pass


class DeviceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_stalls_can_recover_on_third_fresh_pipe_with_one_hello(self):
        created=[];messages=[]
        class RecoveringPipe(FakePipe):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs);self.closed=False;created.append(self)
            async def start(self):
                if len(created)<3:raise TimeoutError('stalled')
            async def close(self):self.closed=True
        async def send(message):messages.append(message)
        session=DeviceSession('/tmp',send,lambda:None,authenticated=True,make_pipe=RecoveringPipe)
        hello={'type':'hello','version':1,'transport':'websocket','audio_params':{'format':'opus','sample_rate':16000,'channels':1,'frame_duration':60}}
        await session.hello(hello,'1','same-device-session')
        self.assertEqual(len(created),3);self.assertEqual(len(messages),1)
        self.assertTrue(all(p.closed for p in created[:2]));self.assertFalse(created[2].closed)
        await session.close()

    async def test_hello_is_acknowledged_while_media_is_connecting(self):
        started, release = asyncio.Event(), asyncio.Event()
        messages = []
        class SlowPipe(FakePipe):
            async def start(self):
                started.set()
                await release.wait()
        async def send(message):
            messages.append(message)
        session = DeviceSession("/tmp", send, lambda: None, authenticated=True, make_pipe=SlowPipe)
        hello = {"type": "hello", "version": 1, "transport": "websocket", "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
        opening = asyncio.create_task(session.hello(hello, "1", "test-session"))
        await started.wait()
        self.assertEqual(messages[0]["type"], "hello")
        release.set()
        await opening
        self.assertEqual(len(messages), 1)
        await session.close()

    async def test_timeout_retries_once_with_fresh_pipe_and_cleans_up(self):
        created = []
        messages = []

        class IntermittentPipe(FakePipe):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.closed = False
                created.append(self)

            async def start(self):
                if self is created[0]:
                    raise TimeoutError("control channel unavailable")

            async def close(self):
                self.closed = True

        async def send(message):
            messages.append(message)

        session = DeviceSession("/tmp", send, lambda: None, authenticated=True, make_pipe=IntermittentPipe)
        hello = {"type": "hello", "version": 1, "transport": "websocket", "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
        await session.hello(hello, "1", "local-session")
        self.assertEqual(len(created), 2)
        self.assertTrue(created[0].closed)
        self.assertFalse(created[1].closed)
        self.assertEqual(len(messages), 1)
        await session.close()
        self.assertTrue(created[1].closed)

    async def test_non_timeout_does_not_retry(self):
        created = []
        class BrokenPipe(FakePipe):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)
            async def start(self):
                raise PermissionError("login required")
        async def send(_):
            pass
        session = DeviceSession("/tmp", send, lambda: None, authenticated=True, make_pipe=BrokenPipe)
        hello = {"type": "hello", "version": 1, "transport": "websocket", "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
        with self.assertRaises(PermissionError):
            await session.hello(hello, "1", "local-session")
        self.assertEqual(len(created), 1)

    async def test_repeated_timeout_closes_all_three_attempts(self):
        created = []
        class TimedOutPipe(FakePipe):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.closed = False
                created.append(self)
            async def start(self):
                raise TimeoutError("unavailable")
            async def close(self):
                self.closed = True
        async def send(_):
            pass
        session = DeviceSession("/tmp", send, lambda: None, authenticated=True, make_pipe=TimedOutPipe)
        hello = {"type": "hello", "version": 1, "transport": "websocket", "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
        with self.assertRaises(TimeoutError):
            await session.hello(hello, "1", "local-session")
        self.assertEqual(len(created), 3)
        self.assertTrue(all(pipe.closed for pipe in created))
        self.assertTrue(session.closed)

    async def test_rejects_unverified_device_before_start(self):
        with self.assertRaises(PermissionError):
            DeviceSession("/tmp", lambda _: None, lambda: None, authenticated=False, make_pipe=FakePipe)

    async def test_hello_listen_audio_and_abort(self):
        messages = []
        interrupts = []

        async def send(data):
            messages.append(data)

        async def interrupt():
            interrupts.append(True)

        session = DeviceSession("/tmp", send, interrupt, authenticated=True, make_pipe=FakePipe)
        hello = {"type": "hello", "version": 2, "transport": "websocket", "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
        await session.hello(hello, "2", "local-session")
        self.assertEqual(messages[0]["type"], "hello")
        await session.device_message({"type": "listen", "state": "start"})
        await session.device_audio(pack_audio(2, AudioPacket(b"abc", 17)))
        self.assertEqual(session.pipe.received, [b"abc"])
        await session.output_start()
        await session.device_message({"type": "abort"})
        self.assertTrue(session.pipe.interrupted)
        self.assertEqual(interrupts, [True])
        self.assertEqual([m["state"] for m in messages[1:] if isinstance(m, dict)], ["start", "stop"])
        await session.close()

    async def test_wrong_header_and_non_16k_fail_closed(self):
        async def send(_):
            raise AssertionError("must not send")

        hello = {"type": "hello", "version": 1, "transport": "websocket", "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
        session = DeviceSession("/tmp", send, lambda: None, authenticated=True, make_pipe=FakePipe)
        with self.assertRaises(ValueError):
            await session.hello(hello, "2", "local-session")
        hello["audio_params"]["sample_rate"] = 24000
        with self.assertRaises(ValueError):
            await session.hello(hello, "1", "local-session")

    async def test_interrupt_during_finish_does_not_emit_duplicate_stop(self):
        started = asyncio.Event()
        release = asyncio.Event()
        messages = []

        class SlowFinishPipe(FakePipe):
            async def finish_output(self):
                started.set()
                await release.wait()

        async def send(data):
            messages.append(data)

        async def interrupt():
            pass

        session = DeviceSession("/tmp", send, interrupt, authenticated=True, make_pipe=SlowFinishPipe)
        hello = {"type": "hello", "version": 1, "transport": "websocket", "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
        await session.hello(hello, "1", "local-session")
        await session.output_start()
        finish = asyncio.create_task(session.output_finish())
        await started.wait()
        await session.interrupt()
        release.set()
        await finish
        self.assertEqual([m.get("state") for m in messages if isinstance(m, dict)].count("stop"), 1)
        await session.close()

    async def test_interrupt_during_output_start_does_not_resume_playback(self):
        started, release = asyncio.Event(), asyncio.Event()
        messages = []

        async def send(data):
            messages.append(data)
            if isinstance(data, dict) and data.get("state") == "start":
                started.set()
                await release.wait()

        async def interrupt():
            pass

        session = DeviceSession("/tmp", send, interrupt, authenticated=True, make_pipe=FakePipe)
        hello = {"type": "hello", "version": 1, "transport": "websocket", "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
        await session.hello(hello, "1", "local-session")
        start = asyncio.create_task(session.output_start())
        await started.wait()
        await session.interrupt()
        release.set()
        await start
        self.assertFalse(session.output_active)
        self.assertTrue(session.pipe.interrupted)
        self.assertEqual([m.get("state") for m in messages if isinstance(m, dict)].count("stop"), 1)
        await session.close()
