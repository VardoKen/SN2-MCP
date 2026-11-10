"""Изолированные проверки завершения точных операций при отмене probe."""

import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from probe_cleanup import CleanupTimeout, settle_task


class ProbeCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_task_returns_exact_receipt(self):
        receipt = {"pid": 23, "creation_time": "2026-10-31T00:00:00.0000000Z"}
        async def operation():
            return receipt
        task = asyncio.create_task(operation())
        await task
        result = await settle_task(task, 1, "completed_start")
        self.assertIs(result, receipt)

    async def _cancel_scenario(self, count):
        started = asyncio.Event()
        released = asyncio.Event()
        observed = {"done": False, "cancelled": False, "receipt": None}
        async def operation():
            started.set()
            try:
                await released.wait()
                observed["receipt"] = {"pid": 25, "executable": "E:/Game/Exact.exe"}
                observed["done"] = True
                return observed["receipt"]
            except asyncio.CancelledError:
                observed["cancelled"] = True
                raise
        inner = asyncio.create_task(operation())
        outer = asyncio.create_task(settle_task(inner, 2, "owned_start"))
        await started.wait()
        for _ in range(count):
            outer.cancel()
            # Позволить каждому запросу отмены войти в settle_task.
            await asyncio.sleep(.01)
        self.assertFalse(inner.cancelled())
        self.assertFalse(observed["done"])
        released.set()
        with self.assertRaises(asyncio.CancelledError):
            await outer
        self.assertTrue(observed["done"])
        self.assertFalse(observed["cancelled"])
        self.assertEqual(observed["receipt"]["pid"], 25)
        self.assertTrue(inner.done())

    async def test_single_cancellation_keeps_start_receipt_before_propagation(self):
        await self._cancel_scenario(1)

    async def test_repeated_cancellation_does_not_interrupt_owned_cleanup(self):
        await self._cancel_scenario(3)

    async def test_child_error_remains_primary_after_deferred_cancel(self):
        started = asyncio.Event()
        released = asyncio.Event()
        failure = ValueError("Exact operation failure")
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        warnings = []
        loop.set_exception_handler(lambda _loop, context: warnings.append(context))
        self.addCleanup(loop.set_exception_handler, previous_handler)
        async def operation():
            started.set()
            await released.wait()
            raise failure
        inner = asyncio.create_task(operation())
        outer = asyncio.create_task(settle_task(inner, 2, "failed_cleanup"))
        await started.wait()
        outer.cancel()
        await asyncio.sleep(.01)
        released.set()
        with self.assertRaises(ValueError) as captured:
            await outer
        self.assertIs(captured.exception, failure)
        self.assertTrue(captured.exception.__notes__)
        await asyncio.sleep(.01)
        self.assertEqual(warnings, [], "Отложенная отмена не должна оставлять необработанную ошибку shield-future")

    async def test_deadline_is_bounded_and_cancels_incomplete_child(self):
        cancelled = asyncio.Event()
        async def operation():
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        task = asyncio.create_task(operation())
        before = asyncio.get_running_loop().time()
        with self.assertRaises(CleanupTimeout) as captured:
            await settle_task(task, .03, "hung_stop")
        self.assertLess(asyncio.get_running_loop().time() - before, 1.5)
        self.assertEqual(captured.exception.code, "cleanup_timeout")
        self.assertTrue(captured.exception.outcome_unknown)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(task.cancelled())

    async def test_child_self_cancellation_does_not_hang(self):
        async def operation():
            raise asyncio.CancelledError()
        task = asyncio.create_task(operation())
        with self.assertRaises(asyncio.CancelledError):
            await settle_task(task, 1, "self_cancelled_operation")
        self.assertTrue(task.cancelled())

    async def test_cancellation_arriving_with_completed_receipt_is_not_lost(self):
        async def operation():
            return {"pid": 27}
        task = asyncio.create_task(operation())
        real_wait = asyncio.wait
        async def cancel_after_done(tasks, **kwargs):
            await real_wait(tasks, **kwargs)
            raise asyncio.CancelledError()
        with patch("probe_cleanup.asyncio.wait", new=cancel_after_done):
            with self.assertRaises(asyncio.CancelledError):
                await settle_task(task, 1, "completion_cancel_race")
        self.assertTrue(task.done())
        self.assertEqual(task.result(), {"pid": 27})

    async def test_invalid_limits_rejected_without_starting_duplicate_operation(self):
        async def operation():
            return 1
        for value in (0, -1, 121, float("nan"), True):
            task = asyncio.create_task(operation())
            with self.subTest(timeout=value), self.assertRaises((ValueError, TypeError)):
                await settle_task(task, value, "invalid_timeout")
            await task


if __name__ == "__main__":
    unittest.main()
