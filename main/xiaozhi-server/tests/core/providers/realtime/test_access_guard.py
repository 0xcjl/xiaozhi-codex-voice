import asyncio
import types
import unittest
from unittest.mock import AsyncMock
from core.providers.realtime.service import run_with_access_guard


class AccessGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_revocation_cancels_and_cleans_running_voice(self):
        released = asyncio.Event()
        async def run():
            try:
                await asyncio.Future()
            finally:
                released.set()
        connection = types.SimpleNamespace(run=run)
        verify = AsyncMock(side_effect=PermissionError("revoked"))
        with self.assertRaises(PermissionError):
            await run_with_access_guard(connection, verify, interval=0.001)
        self.assertTrue(released.is_set())

    async def test_management_failure_also_cancels_voice(self):
        async def run():
            await asyncio.sleep(10)
        connection = types.SimpleNamespace(run=run)
        with self.assertRaises(TimeoutError):
            await run_with_access_guard(connection, AsyncMock(side_effect=TimeoutError()), interval=0.001)

    async def test_normal_close_does_not_leave_guard_running(self):
        verify = AsyncMock()
        await run_with_access_guard(types.SimpleNamespace(run=AsyncMock()), verify, interval=10)
        verify.assert_not_awaited()
