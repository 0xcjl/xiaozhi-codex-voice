"""aiortc 1.15 compatibility for authenticated SCTP simultaneous open.

RFC 9260 5.2.1 requires answering INIT while COOKIE-WAIT/COOKIE-ECHOED.
aiortc handles INIT/COOKIE-ECHO only on its passive side. Reuse those handlers,
including cookie authentication, on the already authenticated DTLS association.
The temporary handler role is task-local; channel parity uses the DTLS role.
Recheck this adapter when upgrading aiortc.
"""
from contextvars import ContextVar

from aiortc.rtcsctptransport import RTCSctpTransport,InitChunk,CookieEchoChunk


class CollisionSafeSctpTransport(RTCSctpTransport):
    def __init__(self,*args,**kwargs):
        self._responding=ContextVar('sctp_handshake_response',default=False)
        self._peer_init=None
        self.simultaneous_inits=0
        self._channel_parity_set=False
        super().__init__(*args,**kwargs)

    @property
    def is_server(self):
        return self._responding.get() or super().is_server

    async def _data_channel_flush(self):
        # RFC 8832 uses the negotiated DTLS role, not the ICE role, for DCEP
        # stream parity. Choose once, before the first automatic stream ID.
        role=getattr(self.transport,'_role',None)
        if (not self._channel_parity_set and self._association_state==self.State.ESTABLISHED
                and role in ('client','server')):
            self._data_channel_id=0 if role=='client' else 1
            self._channel_parity_set=True
        return await super()._data_channel_flush()

    async def _receive_chunk(self,chunk):
        passive=super().is_server
        pending=self._association_state in (self.State.COOKIE_WAIT,self.State.COOKIE_ECHOED)
        respond=False
        if not passive and isinstance(chunk,InitChunk) and pending:
            identity=(chunk.initiate_tag,chunk.initial_tsn)
            if self._peer_init is not None and identity!=self._peer_init:return
            if self._remote_verification_tag not in (0,chunk.initiate_tag):return
            if not chunk.initiate_tag or not chunk.outbound_streams or not chunk.inbound_streams:return
            self._peer_init=identity;self.simultaneous_inits+=1;respond=True
        elif (not passive and isinstance(chunk,CookieEchoChunk) and self._peer_init is not None
              and (pending or self._association_state==self.State.ESTABLISHED)):
            respond=True
        if not respond:
            return await super()._receive_chunk(chunk)
        token=self._responding.set(True)
        try:
            await super()._receive_chunk(chunk)
        finally:
            self._responding.reset(token)
        if isinstance(chunk,CookieEchoChunk) and self._association_state==self.State.ESTABLISHED:
            self._t1_cancel()
