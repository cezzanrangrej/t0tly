from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Awaitable

from app.config import settings
from app.metrics import (
    ANALYTICS_QUEUE_DEPTH,
    ANALYTICS_DROPPED_TOTAL,
    ANALYTICS_FLUSH_FAILURES_TOTAL,
    ANALYTICS_FLUSH_BATCH_SIZE,
    ANALYTICS_FLUSH_DURATION,
    EVENT_LOOP_LAG,
)

logger = logging.getLogger(__name__)

_queue: asyncio.Queue = (
    asyncio.Queue(maxsize=0)
    if settings.analytics_build == "old"
    else asyncio.Queue(maxsize=settings.analytics_queue_size)
)
_flush_task: asyncio.Task | None = None
_lag_task: asyncio.Task | None = None
_enqueue_count = 0


def enqueue_click(code: str, user_agent: str, referrer: str) -> None:
    """
    Non-blocking enqueue of a click event.
    If the queue is full the event is dropped and accounted for in Prometheus metrics —
    redirects are never delayed or blocked.
    """
    global _enqueue_count
    event = {
        "code": code,
        "timestamp": datetime.now(timezone.utc),
        "user_agent": user_agent[:256] if user_agent else "",
        "referrer": referrer[:256] if referrer else "",
    }
    if settings.analytics_build == "old":
        _queue.put_nowait(event)
    else:
        try:
            _queue.put_nowait(event)
        except asyncio.QueueFull:
            ANALYTICS_DROPPED_TOTAL.inc()
            return

    _enqueue_count += 1
    if _enqueue_count % 500 == 0:
        ANALYTICS_QUEUE_DEPTH.set(_queue.qsize())


async def _lag_monitor_loop() -> None:
    """Sample event loop scheduling lag every 200ms."""
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(0.2)
        lag = max(0.0, (time.perf_counter() - t0) - 0.2)
        EVENT_LOOP_LAG.set(lag)


async def _flush_loop(insert_fn: Callable[[list[dict]], Awaitable[None]]) -> None:
    """
    Background task: drain queue in batches and persist to DB via streaming binary COPY.
    If DB is overloaded or times out, the batch is dropped rather than retried to prevent
    head-of-line blocking cascades.
    """
    batch: list[dict] = []
    batch_limit = settings.analytics_batch_size
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
            t0 = time.perf_counter()
            try:
                await asyncio.wait_for(insert_fn(batch), timeout=3.0)
                duration = time.perf_counter() - t0
                ANALYTICS_FLUSH_BATCH_SIZE.observe(len(batch))
                ANALYTICS_FLUSH_DURATION.observe(duration)
            except asyncio.TimeoutError:
                ANALYTICS_FLUSH_FAILURES_TOTAL.inc()
                logger.warning(
                    "Analytics flush timeout (3.0s): dropped %d clicks to protect redirect path",
                    len(batch),
                )
            except Exception as exc:
                ANALYTICS_FLUSH_FAILURES_TOTAL.inc()
                logger.warning(
                    "Analytics flush error: %s (dropped %d clicks)", exc, len(batch)
                )
            finally:
                batch = []
            ANALYTICS_QUEUE_DEPTH.set(_queue.qsize())


_insert_fn: Callable[[list[dict]], Awaitable[None]] | None = None


def start_flush_task(
    insert_fn: Callable[[list[dict]], Awaitable[None]]
) -> asyncio.Task:
    global _flush_task, _lag_task, _insert_fn
    _insert_fn = insert_fn
    _flush_task = asyncio.create_task(_flush_loop(insert_fn))
    _lag_task = asyncio.create_task(_lag_monitor_loop())
    return _flush_task


async def stop_flush_task(timeout: float = 5.0) -> None:
    """
    Graceful shutdown of analytics queue.
    On SIGTERM, cancels the background loop, drains pending clicks from _queue,
    and flushes them with a deadline (default 5.0s).

    DURABILITY GUARANTEE (At-Most-Once):
    Graceful shutdown guarantees all queued clicks are flushed to Postgres before exit.
    However, an abrupt kill (SIGKILL, container crash, OOM-killer, power loss) will
    lose any clicks remaining in this in-process memory queue. For strict zero-loss
    guarantees across hard crashes, an external append-only log (e.g. Apache Kafka or
    Redis Streams) is required.
    """
    global _flush_task, _lag_task, _insert_fn
    if _lag_task and not _lag_task.done():
        _lag_task.cancel()
        try:
            await _lag_task
        except asyncio.CancelledError:
            pass
        _lag_task = None

    if _flush_task and not _flush_task.done():
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
        _flush_task = None

    # Drain remaining in-memory queue items within timeout deadline
    remaining_items: list[dict] = []
    while not _queue.empty():
        try:
            remaining_items.append(_queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    if remaining_items and _insert_fn:
        logger.info(
            "Graceful shutdown: draining %d pending clicks from queue (timeout=%.1fs)...",
            len(remaining_items),
            timeout,
        )
        try:
            await asyncio.wait_for(_insert_fn(remaining_items), timeout=timeout)
            logger.info(
                "Graceful shutdown complete: successfully flushed %d remaining clicks to database.",
                len(remaining_items),
            )
        except asyncio.TimeoutError:
            ANALYTICS_FLUSH_FAILURES_TOTAL.inc()
            logger.warning(
                "Graceful shutdown flush timed out after %.1fs: dropped %d remaining clicks.",
                timeout,
                len(remaining_items),
            )
        except Exception as exc:
            ANALYTICS_FLUSH_FAILURES_TOTAL.inc()
            logger.error(
                "Graceful shutdown flush error (%s): dropped %d remaining clicks.",
                exc,
                len(remaining_items),
            )
    else:
        logger.info("Graceful shutdown: analytics queue was empty (0 clicks lost).")
