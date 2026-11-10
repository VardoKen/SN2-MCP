"""Bounded completion of probe operations before forwarding cancellation to the caller."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import math
import signal
import threading
import time
from typing import Any

import anyio


class CleanupTimeout(RuntimeError):
    code = "cleanup_timeout"
    outcome_unknown = True


async def settle_task(task: asyncio.Task, timeout: float, label: str) -> Any:
    """Wait for one existing task; cancellation does not repeat its actions.

    External cancellation is forwarded after the task completes, so its processes
    and identities saved in its closure remain available to the owner.
    Once the explicit deadline expires, the operation outcome is unknown.
    """
    if (not isinstance(task, asyncio.Task) or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
            or not 0 < timeout <= 120):
        raise ValueError("A task and a finite deadline of at most 120 seconds are required")
    cancelled = False
    deadline = time.monotonic() + timeout
    current = asyncio.current_task()
    with anyio.CancelScope(shield=True):
        while not task.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                task.cancel()
                # Release the local task; an uncertain external operation is not retried.
                try:
                    await asyncio.wait({task}, timeout=1)
                    if task.done():
                        task.exception() if not task.cancelled() else None
                except BaseException:
                    pass
                raise CleanupTimeout(f"{label}: operation did not complete within {timeout:g} seconds")
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                cancelled = True
                if current is not None:
                    current.uncancel()
                if task.done():
                    break
            except BaseException:
                break
        try:
            result = task.result()
        except BaseException as error:
            if cancelled:
                error.add_note(f"{label}: caller cancellation was deferred until the operation completed")
            raise
    if cancelled:
        raise asyncio.CancelledError(f"{label}: cancelled after confirmed completion")
    return result


@contextmanager
def deferred_sigint():
    """Repeated Ctrl+C cancels the task without interrupting protected cleanup."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    task = asyncio.current_task()
    if task is None:
        yield
        return
    previous = signal.getsignal(signal.SIGINT)

    def cancel(signum, frame):
        task.cancel("SIGINT")

    signal.signal(signal.SIGINT, cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
