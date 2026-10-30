import asyncio
import types
import unittest

from core.providers.realtime.codex_media import wait_transport_ready


class TransportReadyTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_dtls_fails_before_the_whole_startup_deadline(self):
        peer=types.SimpleNamespace(connectionState='connecting',iceConnectionState='completed',
            sctp=types.SimpleNamespace(state='new',transport=types.SimpleNamespace(state='connecting')))
        channel=types.SimpleNamespace(readyState='connecting')
        with self.assertRaisesRegex(TimeoutError,'phase=dtls'):
            await wait_transport_ready(peer,channel,timeout=1,phase_timeout=.005,interval=.001)

    async def test_forward_phase_progress_resets_only_phase_deadline(self):
        dtls=types.SimpleNamespace(state='new');sctp=types.SimpleNamespace(state='new',transport=dtls)
        peer=types.SimpleNamespace(connectionState='connecting',iceConnectionState='checking',sctp=sctp)
        channel=types.SimpleNamespace(readyState='connecting')
        task=asyncio.create_task(wait_transport_ready(peer,channel,timeout=.2,phase_timeout=.04,interval=.001))
        await asyncio.sleep(.02);peer.iceConnectionState='completed';dtls.state='connecting'
        await asyncio.sleep(.02);dtls.state='connected';sctp.state='connecting'
        await asyncio.sleep(.02);sctp.state='connected'
        await asyncio.sleep(.02);channel.readyState='open';peer.connectionState='connected'
        await task

    async def test_phase_policy_does_not_extend_total_startup_budget(self):
        peer=types.SimpleNamespace(connectionState='connecting',iceConnectionState='completed',
            sctp=types.SimpleNamespace(state='new',transport=types.SimpleNamespace(state='connecting')))
        channel=types.SimpleNamespace(readyState='connecting')
        with self.assertRaises(TimeoutError):
            await wait_transport_ready(peer,channel,timeout=.005,phase_timeout=1,interval=.001)

    async def test_waits_for_peer_and_data_channel(self):
        peer = types.SimpleNamespace(connectionState="connecting")
        channel = types.SimpleNamespace(readyState="connecting")
        task = asyncio.create_task(wait_transport_ready(peer, channel, timeout=1, interval=0.001))
        await asyncio.sleep(0.005)
        self.assertFalse(task.done())
        peer.connectionState = "connected"
        await asyncio.sleep(0.005)
        self.assertFalse(task.done())
        channel.readyState = "open"
        await task

    async def test_fails_when_connection_closes_or_times_out(self):
        peer = types.SimpleNamespace(connectionState="failed")
        channel = types.SimpleNamespace(readyState="connecting")
        with self.assertRaisesRegex(RuntimeError, "failed"):
            await wait_transport_ready(peer, channel, timeout=1, interval=0.001)
        peer.connectionState = "connecting"
        with self.assertRaisesRegex(TimeoutError, "ready"):
            await wait_transport_ready(peer, channel, timeout=0.005, interval=0.001)
