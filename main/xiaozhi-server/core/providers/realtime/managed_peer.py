"""Own aiortc 1.15 connection jobs so shutdown cannot leave them running.

aiortc 1.15 schedules its private __connect coroutine without retaining its
Task, and close() does not join it. This version-specific wrapper serializes
those jobs, records failures for the caller, and joins them before ICE closes.
It must be rechecked when upgrading aiortc.
"""

import asyncio

from aiortc import RTCPeerConnection,RTCBundlePolicy
from .collision_safe_sctp import CollisionSafeSctpTransport


class ManagedPeerConnection(RTCPeerConnection):
    def _RTCPeerConnection__createSctpTransport(self):
        # Version-pinned equivalent of aiortc's factory, using our collision
        # handler without replacing classes globally or modifying site-packages.
        transceivers=self._RTCPeerConnection__transceivers
        bundled=(self._RTCPeerConnection__configuration.bundlePolicy==RTCBundlePolicy.MAX_BUNDLE
                 and bool(transceivers))
        dtls=(transceivers[0].receiver.transport if bundled
              else self._RTCPeerConnection__createDtlsTransport())
        sctp=CollisionSafeSctpTransport(dtls)
        sctp._bundled=bundled;sctp.mid=None
        self._RTCPeerConnection__sctp=sctp
        @sctp.on('datachannel')
        def on_datachannel(channel):self.emit('datachannel',channel)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connect_tasks = set()
        self.connect_failure = None
        self.connect_lock = asyncio.Lock()
        self.closing = False

    async def _RTCPeerConnection__connect(self):
        task = asyncio.current_task()
        self.connect_tasks.add(task)
        try:
            async with self.connect_lock:
                if not self.closing:
                    await super()._RTCPeerConnection__connect()
        except Exception as error:
            # The transport's readiness check and service watcher treat this
            # as fatal. No provider content or SDP is retained in the diagnostic.
            self.connect_failure = type(error).__name__
        finally:
            self.connect_tasks.discard(task)

    async def close(self):
        self.closing = True
        tasks = [task for task in self.connect_tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # aioice.connect() cancels its candidate checks only on normal return.
        # Cancellation otherwise leaves STUN retry timers writing to closed UDP.
        checks=set()
        for ice in self._RTCPeerConnection__iceTransports:
            connection=ice._connection
            # No new triggered checks while shutdown joins the existing ones.
            connection.check_start_task=lambda pair:None
            checks.update(pair.task for pair in connection._check_list
                          if pair.task is not None and pair.task is not asyncio.current_task())
        for task in checks:task.cancel()
        await asyncio.gather(*checks,return_exceptions=True)
        await super().close()
