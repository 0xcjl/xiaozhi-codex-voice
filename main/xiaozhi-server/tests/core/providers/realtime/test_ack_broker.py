import asyncio
import unittest

from core.providers.realtime.ack_broker import AckBroker, BrokerError


class AckBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_serializes_concurrent_tasks_and_maps_receipts(self):
        sent = []
        broker = AckBroker(send=sent.append, timeout=1)
        first = asyncio.create_task(broker.submit("task", "a", "first"))
        second = asyncio.create_task(broker.submit("task", "b", "second"))
        await asyncio.sleep(0)
        self.assertEqual(len(sent), 1)
        broker.receive({"type": "session.context.appended", "start_ms": 0, "end_ms": 200})
        self.assertEqual((await first)["task_id"], "a")
        await asyncio.sleep(0)
        self.assertEqual(len(sent), 2)
        broker.receive({"type": "session.context.appended", "start_ms": 200, "end_ms": 400})
        self.assertEqual((await second)["task_id"], "b")
        broker.close()

    async def test_unsolicited_ack_blocks_further_operations(self):
        broker = AckBroker(send=lambda e: None, timeout=1)
        broker.receive({"type": "session.context.appended", "start_ms": 0, "end_ms": 200})
        with self.assertRaises(BrokerError):
            await broker.submit("task", "a", "do not dispatch")

    async def test_stale_ack_does_not_release_next_task(self):
        sent = []
        broker = AckBroker(send=sent.append, timeout=1)
        first = asyncio.create_task(broker.submit("task", "a", "first"))
        second = asyncio.create_task(broker.submit("task", "b", "second"))
        await asyncio.sleep(0)
        ack = {"type": "session.context.appended", "start_ms": 0, "end_ms": 200}
        broker.receive(ack)
        await first
        await asyncio.sleep(0)
        broker.receive(ack)
        with self.assertRaises(BrokerError):
            await second
        broker.close()

    async def test_missing_ack_times_out_and_closes_queue(self):
        broker = AckBroker(send=lambda e: None, timeout=0.01)
        with self.assertRaises(BrokerError):
            await broker.submit("task", "a", "first")
        self.assertTrue(broker.fault)

    async def test_nonfinite_ack_time_is_rejected(self):
        broker = AckBroker(send=lambda e: None, timeout=1)
        task = asyncio.create_task(broker.submit("task", "a", "first"))
        await asyncio.sleep(0)
        broker.receive({"type": "session.context.appended", "start_ms": 0, "end_ms": float("nan")})
        with self.assertRaises(BrokerError):
            await task
