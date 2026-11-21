from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

from app.config import settings
from app.metrics import ANALYTICS_QUEUE_DEPTH

logger = logging.getLogger(__name__)

_queue: asyncio.Queue = asyncio.Queue(maxsize=settings.analytics_queue_size)
_flush_task: asyncio.Task | None = None
_enqueue_count = 0


def enqueue_click(code: str, user_agent: str, referrer: str) -> None:
    """
    Non-blocking enqueue of a click event.
    If the queue is full the event is silently dropped — redirects are never slowed.
    """
    global _enqueue_count
    event = {
        "code": code,
        "timestamp": datetime.now(timezone.utc),
        "user_agent": user_agent[:256] if user_agent else "",
        "referrer": referrer[:256] if referrer else "",
    }
    try:
        _queue.put_nowait(event)
    except asyncio.QueueFull:
        pass  # intentional drop per requirement 8

    # Sample metric updates to reduce Prometheus lock overhead
    _enqueue_count += 1
    if _enqueue_count % 500 == 0:
        ANALYTICS_QUEUE_DEPTH.set(_queue.qsize())


async def _flush_loop(insert_fn: Callable[[list[dict]], Awaitable[None]]) -> None:
    """Background task: drain queue in batches and persist to DB."""
    batch: list[dict] = []
    batch_limit = 500
    while True:
        try:
            event = await asyncio.wait_for(
                _queue.get(), timeout=settings.analytics_flush_interval
            )
            batch.append(event)
            # Drain up to batch_limit without waiting
            while len(batch) < batch_limit:
                try:
                    batch.append(_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
        except asyncio.TimeoutError:
            pass  # flush whatever we have accumulated

        if batch:
            try:
                await asyncio.wait_for(insert_fn(batch), timeout=3.0)
            except Exception as exc:
                logger.warning("Analytics flush error: %s", exc)
            finally:
                batch = []
            ANALYTICS_QUEUE_DEPTH.set(_queue.qsize())


def start_flush_task(
    insert_fn: Callable[[list[dict]], Awaitable[None]]
) -> asyncio.Task:
    global _flush_task
    _flush_task = asyncio.create_task(_flush_loop(insert_fn))
    return _flush_task


async def stop_flush_task() -> None:
    global _flush_task
    if _flush_task and not _flush_task.done():
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
        _flush_task = None
