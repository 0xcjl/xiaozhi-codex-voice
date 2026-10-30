import asyncio
import unittest
import types
from unittest.mock import AsyncMock, patch

try:
    from aiortc import RTCPeerConnection
    from core.providers.realtime.codex_media import wait_transport_ready
    from core.providers.realtime.managed_peer import ManagedPeerConnection
    HAVE_MEDIA = True
except ImportError:
    HAVE_MEDIA = False


@unittest.skipUnless(HAVE_MEDIA, "optional aiortc dependency is required")
class ManagedPeerTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_joins_owned_ice_checks_before_closing_udp(self):
        peer=ManagedPeerConnection()
        peer.createDataChannel('test')
        connection=peer.sctp.transport.transport._connection
        stopped=asyncio.Event();started=asyncio.Event()
        async def check():
            started.set()
            try:await asyncio.Future()
            finally:stopped.set()
        task=asyncio.create_task(check());await started.wait()
        connection._check_list.append(types.SimpleNamespace(task=task))
        try:
            await peer.close()
            self.assertTrue(stopped.is_set())
            self.assertTrue(task.done())
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_real_pending_stun_transaction_is_joined_before_socket_close(self):
        from aioice.ice import StunProtocol,CandidatePair
        from aioice import Candidate
        peer=ManagedPeerConnection();peer.createDataChannel('test')
        connection=peer.sctp.transport.transport._connection
        connection.remote_username='isolated-peer';connection.remote_password='isolated-test-password'
        loop=asyncio.get_running_loop();errors=[];previous=loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop,context:errors.append(context))
        udp,protocol=await loop.create_datagram_endpoint(lambda:StunProtocol(connection),local_addr=('127.0.0.1',0))
        protocol.local_candidate=Candidate('local',1,'udp',1,'127.0.0.1',udp.get_extra_info('sockname')[1],'host')
        connection._protocols.append(protocol)
        pair=CandidatePair(protocol,Candidate('discard',1,'udp',1,'127.0.0.1',9,'host'))
        connection._check_list.append(pair);connection.check_start_task(pair)
        try:
            await asyncio.sleep(.02);self.assertTrue(protocol.transactions)
            await peer.close()
            self.assertTrue(pair.task.done());self.assertFalse(protocol.transactions)
            await asyncio.sleep(.6)
            self.assertFalse(errors)
        finally:
            await peer.close();loop.set_exception_handler(previous)

    async def test_peer_close_failure_still_closes_owned_session(self):
        from core.providers.realtime.codex_media import CodexMediaTransport
        from pathlib import Path
        async def consume(_):
            pass
        transport = CodexMediaTransport(Path.cwd(), consume)
        original_close = transport.peer.close
        close_session = AsyncMock()
        try:
            with patch.object(transport.peer, "close", AsyncMock(side_effect=RuntimeError("synthetic close failure"))), patch.object(
                transport.session, "close", close_session
            ):
                with self.assertRaises(RuntimeError):
                    await transport.close()
                close_session.assert_awaited_once()
        finally:
            await original_close()

    async def test_close_cancels_and_joins_inflight_connect(self):
        started = asyncio.Event()
        async def connect(_):
            started.set()
            await asyncio.Event().wait()
        peer = ManagedPeerConnection()
        with patch.object(RTCPeerConnection, "_RTCPeerConnection__connect", connect):
            task = asyncio.create_task(peer._RTCPeerConnection__connect())
            await started.wait()
            await peer.close()
        self.assertTrue(task.done())
        self.assertEqual(peer.connect_tasks, set())
        self.assertIsNone(peer.connect_failure)

    async def test_connect_failure_is_visible_to_readiness_check(self):
        async def fail(_):
            raise RuntimeError("synthetic failure")
        peer = ManagedPeerConnection()
        try:
            with patch.object(RTCPeerConnection, "_RTCPeerConnection__connect", fail):
                await peer._RTCPeerConnection__connect()
            self.assertEqual(peer.connect_failure, "RuntimeError")
            with self.assertRaisesRegex(RuntimeError, "connection task"):
                await wait_transport_ready(peer, type("Channel", (), {"readyState": "connecting"})())
        finally:
            await peer.close()
