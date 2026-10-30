"""Serialize realtime context updates when acknowledgments lack request IDs.

This assumes exactly one context writer. Unknown, late or ambiguous receipts
fail closed; they are never assigned to a different task by guessing.
"""

import asyncio
import inspect
import math
from collections import deque


class BrokerError(RuntimeError):
    pass


class AckBroker:
    def __init__(self, send, timeout=8):
        self.send = send
        self.timeout = timeout
        self.queue = deque()
        self.current = None
        self.last_end = -1
        self.serial = 0
        self.fault = None
        self.timer = None

    async def submit(self, kind, task_id, text, *, priority=False, dispatch=None):
        if self.fault:
            raise self.fault
        self.serial += 1
        future = asyncio.get_running_loop().create_future()
        item = {"id": f"ctx-{self.serial}", "kind": kind, "task_id": task_id,
                "text": text, "future": future, "dispatch": dispatch}
        (self.queue.appendleft if priority else self.queue.append)(item)
        self._pump()
        return await future

    def _pump(self):
        if self.fault or self.current:
            return
        while self.queue and self.queue[0]["future"].cancelled():
            self.queue.popleft()
        if not self.queue:
            return
        item = self.queue.popleft()
        self.current = item
        self.timer = asyncio.get_running_loop().call_later(self.timeout, self._fail, "context acknowledgment timed out")
        try:
            result = item["dispatch"]() if item["dispatch"] else self.send({
                "type": "session.context.append", "event_id": item["id"], "channel": "commentary",
                "content": [{"type": "input_text", "text": item["text"]}],
            })
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                task.add_done_callback(self._dispatch_finished)
        except Exception:
            self._fail("context dispatch failed")

    def _dispatch_finished(self, task):
        if task.cancelled() or task.exception() is not None:
            self._fail("context dispatch failed")

    def receive(self, event):
        if self.fault:
            return
        if event.get("type") == "error" and self.current and event.get("error", {}).get("event_id") == self.current["id"]:
            self._fail("server rejected context append")
            return
        if event.get("type") != "session.context.appended":
            return
        if not self.current:
            self._fail("unsolicited or duplicate context acknowledgment")
            return
        item = self.current
        event_id = event.get("client_event_id")
        end = event.get("end_ms")
        start = event.get("start_ms")
        if event_id and event_id != item["id"]:
            self._fail("mismatched context acknowledgment")
            return
        if not event_id and (type(start) not in (int, float) or type(end) not in (int, float) or not math.isfinite(start) or not math.isfinite(end) or end <= self.last_end):
            self._fail("ambiguous or out-of-order context acknowledgment")
            return
        if type(end) in (int, float) and math.isfinite(end):
            self.last_end = max(self.last_end, end)
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.current = None
        if not item["future"].done():
            item["future"].set_result({"id": item["id"], "kind": item["kind"], "task_id": item["task_id"], "correlated": bool(event_id), "end_ms": end})
        self._pump()

    def _fail(self, reason):
        if self.fault:
            return
        self.fault = BrokerError(reason)
        if self.timer:
            self.timer.cancel()
            self.timer = None
        items = ([self.current] if self.current else []) + list(self.queue)
        self.current = None
        self.queue.clear()
        for item in items:
            if not item["future"].done():
                item["future"].set_exception(self.fault)

    def close(self):
        self._fail("context broker closed")
