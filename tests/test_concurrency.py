from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.idgen import BlockAllocator, id_to_code, CODE_LENGTH


class MockAcquire:
    """Async context manager that returns a mock DB connection."""
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_10k_unique_codes():
    """
    10,000 concurrent next_id() calls must produce 10,000 unique IDs and codes.
    The block allocator should request exactly ceil(10000/100000) = 1 block.
    """
    block = 100_000
    call_count = 0

    async def mock_fetchrow(_query):
        nonlocal call_count
        call_count += 1
        last = call_count * block
        row = MagicMock()
        row.__getitem__ = MagicMock(side_effect=lambda k: last if k == "v" else None)
        return row

    mock_conn = AsyncMock()
    mock_conn.fetchrow = mock_fetchrow

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=MockAcquire(mock_conn))

    # Use an isolated BlockAllocator instance so global allocator is untouched
    test_allocator = BlockAllocator()
    test_allocator.set_pool(mock_pool)

    # Fan out 10,000 concurrent ID requests
    ids = await asyncio.gather(*[test_allocator.next_id() for _ in range(10_000)])
    codes = [id_to_code(i) for i in ids]

    assert len(set(ids)) == 10_000, f"Expected 10000 unique IDs, got {len(set(ids))}"
    assert len(set(codes)) == 10_000, f"Expected 10000 unique codes, got {len(set(codes))}"

    for code in codes:
        assert len(code) == CODE_LENGTH, f"Code not {CODE_LENGTH} chars: '{code}'"

    # With block_size=100k, 10k IDs should fit in exactly 1 block refill
    assert call_count == 1, f"Expected 1 DB call, got {call_count}"


@pytest.mark.asyncio
async def test_block_boundary_triggers_refill():
    """When a block is exhausted, the next call triggers exactly one more refill."""
    block = 100_000
    call_count = 0

    async def mock_fetchrow(_query):
        nonlocal call_count
        call_count += 1
        last = call_count * block
        row = MagicMock()
        row.__getitem__ = MagicMock(side_effect=lambda k: last if k == "v" else None)
        return row

    mock_conn = AsyncMock()
    mock_conn.fetchrow = mock_fetchrow
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=MockAcquire(mock_conn))

    test_allocator = BlockAllocator()
    test_allocator.set_pool(mock_pool)

    # Exhaust one full block
    for _ in range(block):
        await test_allocator.next_id()

    assert call_count == 1

    # One more call should trigger exactly one more refill
    await test_allocator.next_id()
    assert call_count == 2


@pytest.mark.asyncio
async def test_multiple_allocators_concurrent_no_duplicates():
    """
    Simulate 5 independent API workers/containers (5 BlockAllocators)
    leasing blocks from the same atomic database sequence concurrently.
    Asserts zero duplicate IDs and zero duplicate short codes across all workers.
    """
    block = 100_000
    sequence_lock = asyncio.Lock()
    sequence_value = 0

    async def atomic_sequence_fetchrow(_query):
        nonlocal sequence_value
        async with sequence_lock:
            sequence_value += block
            current = sequence_value
        row = MagicMock()
        row.__getitem__ = MagicMock(side_effect=lambda k: current if k == "v" else None)
        return row

    mock_conn = AsyncMock()
    mock_conn.fetchrow = atomic_sequence_fetchrow
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=MockAcquire(mock_conn))

    # Spawn 5 distinct worker allocators
    allocators = [BlockAllocator() for _ in range(5)]
    for a in allocators:
        a.set_pool(mock_pool)

    # Concurrently generate 2,000 IDs per worker (10,000 IDs total)
    async def worker_generate(alloc: BlockAllocator, count: int) -> list[int]:
        return [await alloc.next_id() for _ in range(count)]

    tasks = [worker_generate(alloc, 2_000) for alloc in allocators]
    results = await asyncio.gather(*tasks)

    # Flatten results
    all_ids = [id_val for sublist in results for id_val in sublist]
    all_codes = [id_to_code(id_val) for id_val in all_ids]

    assert len(all_ids) == 10_000, f"Expected 10,000 IDs, got {len(all_ids)}"
    assert len(set(all_ids)) == 10_000, f"Collision detected! Only {len(set(all_ids))} unique IDs"
    assert len(set(all_codes)) == 10_000, f"Feistel collision detected! Only {len(set(all_codes))} unique codes"
