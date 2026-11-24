from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
import app.ratelimit as rl


@pytest.fixture(autouse=True)
def reset_redis():
    """Restore original redis after each test."""
    orig = rl._redis
    yield
    rl._redis = orig


def _make_redis_mock(count: int, ttl: int) -> AsyncMock:
    mock_pipe = AsyncMock()
    mock_pipe.execute = AsyncMock(return_value=[count, ttl])
    mock_pipe.incr = MagicMock(return_value=mock_pipe)
    mock_pipe.ttl = MagicMock(return_value=mock_pipe)
    mock_redis = AsyncMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)
    mock_redis.expire = AsyncMock(return_value=True)
    return mock_redis


@pytest.mark.asyncio
async def test_allows_first_request():
    rl.set_redis(_make_redis_mock(count=1, ttl=60))
    allowed, retry = await rl.check_rate_limit("1.2.3.4")
    assert allowed is True
    assert retry == 0


@pytest.mark.asyncio
async def test_allows_at_limit():
    rl.set_redis(_make_redis_mock(count=10, ttl=30))
    allowed, retry = await rl.check_rate_limit("1.2.3.4")
    assert allowed is True


@pytest.mark.asyncio
async def test_blocks_over_limit():
    rl.set_redis(_make_redis_mock(count=11, ttl=45))
    allowed, retry = await rl.check_rate_limit("1.2.3.4")
    assert allowed is False
    assert retry == 45


@pytest.mark.asyncio
async def test_sets_ttl_on_new_key():
    """When ttl == -2 (key just created), expire should be called."""
    mock_redis = _make_redis_mock(count=1, ttl=-2)
    rl.set_redis(mock_redis)
    allowed, _ = await rl.check_rate_limit("5.6.7.8")
    assert allowed is True
    mock_redis.expire.assert_awaited()


@pytest.mark.asyncio
async def test_fails_open_on_redis_error():
    mock_pipe = AsyncMock()
    mock_pipe.incr = MagicMock(return_value=mock_pipe)
    mock_pipe.ttl = MagicMock(return_value=mock_pipe)
    mock_pipe.execute = AsyncMock(side_effect=Exception("Redis down"))
    mock_redis = AsyncMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)
    rl.set_redis(mock_redis)
    allowed, retry = await rl.check_rate_limit("1.2.3.4")
    assert allowed is True


@pytest.mark.asyncio
async def test_allows_when_redis_none():
    rl.set_redis(None)
    allowed, retry = await rl.check_rate_limit("1.2.3.4")
    assert allowed is True
    assert retry == 0
