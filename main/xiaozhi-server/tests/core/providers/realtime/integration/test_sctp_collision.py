import asyncio
import types
import unittest
import copy
from unittest.mock import AsyncMock
from aiortc.rtcsctptransport import RTCSctpTransport,InitChunk,InitAckChunk,CookieEchoChunk,CookieAckChunk,SCTP_STATE_COOKIE
from core.providers.realtime.collision_safe_sctp import CollisionSafeSctpTransport


class SctpCollisionTests(unittest.IsolatedAsyncioTestCase):
    def make(self,cls=CollisionSafeSctpTransport):
        dtls=types.SimpleNamespace(state='new',transport=types.SimpleNamespace(role='controlling'))
        sctp=cls(dtls);sctp._send_chunk=AsyncMock();sctp._association_state=sctp.State.COOKIE_WAIT
        self.instances.append(sctp)
        return sctp
    async def asyncSetUp(self):self.instances=[]
    async def asyncTearDown(self):
        for s in self.instances:s._t1_cancel()
        await asyncio.sleep(0)
    def init(self):
        init=InitChunk();init.initiate_tag=123;init.initial_tsn=456
        init.outbound_streams=100;init.inbound_streams=100;init.advertised_rwnd=65536
        return init
    async def test_upstream_reproduces_ignored_simultaneous_init(self):
        s=self.make(RTCSctpTransport)
        await s._receive_chunk(self.init())
        s._send_chunk.assert_not_awaited()
    async def test_collision_ack_preserves_own_identity_state_and_timer(self):
        s=self.make();s._t1_start(InitChunk());timer=s._t1_handle
        local=(s._local_verification_tag,s._local_tsn)
        await s._receive_chunk(self.init())
        ack=s._send_chunk.call_args.args[0]
        self.assertIsInstance(ack,InitAckChunk)
        self.assertEqual((ack.initiate_tag,ack.initial_tsn),local)
        self.assertEqual(s._association_state,s.State.COOKIE_WAIT)
        self.assertIs(s._t1_handle,timer);self.assertFalse(s.is_server)
    async def test_only_authenticated_cookie_completes_collision_and_cancels_timer(self):
        s=self.make();s._t1_start(InitChunk())
        await s._receive_chunk(self.init())
        ack=s._send_chunk.call_args.args[0]
        cookie=dict(ack.params)[SCTP_STATE_COOKIE]
        await s._receive_chunk(CookieEchoChunk(body=bytes(len(cookie))))
        self.assertEqual(s._association_state,s.State.COOKIE_WAIT)
        await s._receive_chunk(CookieEchoChunk(body=cookie))
        self.assertIsInstance(s._send_chunk.call_args.args[0],CookieAckChunk)
        self.assertEqual(s._association_state,s.State.ESTABLISHED)
        self.assertIsNone(s._t1_handle);self.assertFalse(s.is_server)
    async def test_foreign_init_cannot_replace_pending_identity(self):
        s=self.make();await s._receive_chunk(self.init());s._send_chunk.reset_mock()
        other=self.init();other.initiate_tag+=1
        await s._receive_chunk(other);s._send_chunk.assert_not_awaited()
        self.assertEqual(s._remote_verification_tag,123)
    async def test_cookie_without_prior_collision_cannot_establish(self):
        s=self.make();await s._receive_chunk(CookieEchoChunk(body=b'not-a-cookie'))
        s._send_chunk.assert_not_awaited();self.assertEqual(s._association_state,s.State.COOKIE_WAIT)
    async def test_handler_role_does_not_leak_to_other_async_tasks(self):
        s=self.make();entered=asyncio.Event();release=asyncio.Event()
        async def send(_):entered.set();await release.wait()
        s._send_chunk.side_effect=send
        task=asyncio.create_task(s._receive_chunk(self.init()))
        await entered.wait();self.assertFalse(s.is_server)
        release.set();await task;self.assertFalse(s.is_server)
    async def test_normal_passive_side_still_uses_upstream_cookie_validation(self):
        s=self.make();s.transport.transport.role='controlled'
        await s._receive_chunk(self.init())
        self.assertIsInstance(s._send_chunk.call_args.args[0],InitAckChunk)
        self.assertEqual(s.simultaneous_inits,0)
    async def test_two_active_endpoints_complete_one_collision_without_timer_leaks(self):
        a=self.make();b=self.make();packets=asyncio.Queue()
        async def from_a(chunk):packets.put_nowait((b,copy.deepcopy(chunk)))
        async def from_b(chunk):packets.put_nowait((a,copy.deepcopy(chunk)))
        a._send_chunk.side_effect=from_a;b._send_chunk.side_effect=from_b
        await a._init();await b._init()
        async with asyncio.timeout(1):
            while not packets.empty():
                receiver,chunk=packets.get_nowait();await receiver._receive_chunk(chunk)
        for s in (a,b):
            self.assertEqual(s._association_state,s.State.ESTABLISHED)
            self.assertEqual(s.simultaneous_inits,1)
            self.assertIsNone(s._t1_handle);self.assertFalse(s.is_server)
    async def test_cookie_echoed_duplicate_init_preserves_active_timer(self):
        s=self.make();await s._receive_chunk(self.init())
        s._association_state=s.State.COOKIE_ECHOED;s._t1_start(CookieEchoChunk(body=b'pending'))
        timer=s._t1_handle;await s._receive_chunk(self.init())
        self.assertEqual(s._association_state,s.State.COOKIE_ECHOED)
        self.assertIs(s._t1_handle,timer)
    async def test_expired_authenticated_cookie_cannot_establish(self):
        s=self.make();await s._receive_chunk(self.init())
        cookie=dict(s._send_chunk.call_args.args[0].params)[SCTP_STATE_COOKIE]
        now=s._get_timestamp();s._get_timestamp=lambda:now+100000
        await s._receive_chunk(CookieEchoChunk(body=cookie))
        self.assertNotEqual(s._association_state,s.State.ESTABLISHED)
    async def test_dcep_stream_parity_follows_dtls_instead_of_ice_role(self):
        for dtls_role,ice_role,expected in [('client','controlling',0),('server','controlled',1),
                                           ('client','controlled',0),('server','controlling',1)]:
            s=self.make();s.transport._role=dtls_role;s.transport.transport.role=ice_role
            s._association_state=s.State.ESTABLISHED;s._data_channel_id=99
            await s._data_channel_flush();self.assertEqual(s._data_channel_id,expected)
            s._data_channel_id=expected+2
            await s._data_channel_flush();self.assertEqual(s._data_channel_id,expected+2)
    async def test_first_real_dcep_open_uses_even_id_for_dtls_client(self):
        from aiortc.rtcdatachannel import RTCDataChannel,RTCDataChannelParameters
        s=self.make();s.transport._role='client';s._send=AsyncMock()
        channel=RTCDataChannel(s,RTCDataChannelParameters(label='oai-events'))
        s._data_channel_id=1;s._association_state=s.State.ESTABLISHED
        await s._data_channel_flush()
        self.assertEqual(channel.id,0)
        self.assertEqual(s._send.call_args.args[0],0)
