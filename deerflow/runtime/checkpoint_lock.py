"""Process-wide serialization for checkpoint mutations on one thread.

LangGraph exposes both synchronous and asynchronous execution APIs.  Using an
``asyncio.Lock`` per event loop leaves those APIs free to mutate the same
checkpoint concurrently, so the lock registry deliberately stores ordinary
threading locks and both call styles share it.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import weakref
from collections.abc import AsyncIterator, Iterator

_locks_guard = threading.Lock()
_locks: weakref.WeakValueDictionary[str, threading.Lock] = (
    weakref.WeakValueDictionary()
)


def _lock_for_thread(thread_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(thread_id)
        if lock is None:
            lock = threading.Lock()
            _locks[thread_id] = lock
        return lock


async def acquire_checkpoint_thread_lock(thread_id: str) -> threading.Lock:
    """Acquire the shared lock without blocking the event loop.

    Cancellation needs special handling because ``asyncio.to_thread`` cannot
    cancel a lock acquisition that has already started.  If the waiter is
    cancelled, wait until the worker really acquires the lock and release it
    before propagating cancellation; otherwise the thread would remain wedged.
    """

    lock = _lock_for_thread(thread_id)
    acquisition = asyncio.create_task(asyncio.to_thread(lock.acquire))
    try:
        await asyncio.shield(acquisition)
    except asyncio.CancelledError:
        await acquisition
        lock.release()
        raise
    return lock


@contextlib.contextmanager
def checkpoint_thread_lock_sync(thread_id: str) -> Iterator[None]:
    """Serialize a synchronous checkpoint mutation for ``thread_id``."""

    lock = _lock_for_thread(thread_id)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


@contextlib.asynccontextmanager
async def checkpoint_thread_lock_async(thread_id: str) -> AsyncIterator[None]:
    """Serialize an asynchronous checkpoint mutation for ``thread_id``."""

    lock = await acquire_checkpoint_thread_lock(thread_id)
    try:
        yield
    finally:
        lock.release()
