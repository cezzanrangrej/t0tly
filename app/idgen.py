from __future__ import annotations
import asyncio
from app.config import settings

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
CODE_LENGTH = 7
FEISTEL_ROUNDS = 4

# 62^7 = 3_521_614_606_208 — the full set of 7-char base62 codes.
# The Feistel network works over [0, 2^42) which contains this domain.
# We use cycle-walking to stay strictly within [0, DOMAIN).
DOMAIN = 62**7  # 3_521_614_606_208

# Two 21-bit halves for a balanced Feistel (21 + 21 = 42 bits)
HALF_BITS = 21
HALF_MASK = (1 << HALF_BITS) - 1  # 2_097_151

_round_keys: list[int] = []


def _feistel_round_keys() -> list[int]:
    """
    Derive four 32-bit round keys from environment strings (deterministic hash).
    SECURITY REQUIREMENT:
    These keys MUST remain fixed across all worker processes, containers, and restarts
    to ensure deterministic bijection. They must be injected via environment variables
    and NEVER committed to source control.
    """
    keys = []
    for raw in [
        settings.feistel_key1, settings.feistel_key2,
        settings.feistel_key3, settings.feistel_key4,
    ]:
        h = 0
        for ch in raw.encode():
            h = (h * 31 + ch) & 0xFFFF_FFFF
        keys.append(h)
    return keys


def _get_keys() -> list[int]:
    global _round_keys
    if not _round_keys:
        _round_keys = _feistel_round_keys()
    return _round_keys


def _inner_encrypt(n: int, keys: list[int]) -> int:
    """One pass of the 4-round balanced Feistel over [0, 2^42)."""
    L = (n >> HALF_BITS) & HALF_MASK
    R = n & HALF_MASK
    for i in range(FEISTEL_ROUNDS):
        f = ((R * keys[i] + (R >> 3) ^ keys[i]) & HALF_MASK)
        L, R = R, (L ^ f) & HALF_MASK
    return (L << HALF_BITS) | R


def _inner_decrypt(n: int, keys: list[int]) -> int:
    """Inverse of _inner_encrypt."""
    L = (n >> HALF_BITS) & HALF_MASK
    R = n & HALF_MASK
    for i in reversed(range(FEISTEL_ROUNDS)):
        f = ((L * keys[i] + (L >> 3) ^ keys[i]) & HALF_MASK)
        R, L = L, (R ^ f) & HALF_MASK
    return (L << HALF_BITS) | R


def _feistel_encrypt(n: int) -> int:
    """
    Bijective shuffle of n within [0, DOMAIN) via cycle-walking Feistel.
    Values outside DOMAIN are re-encrypted until they land inside; the
    expected number of extra iterations is tiny (< 20% rejection rate).
    """
    keys = _get_keys()
    result = n
    while True:
        result = _inner_encrypt(result, keys)
        if result < DOMAIN:
            return result


def _feistel_decrypt(n: int) -> int:
    """Inverse of _feistel_encrypt (cycle-walk in reverse)."""
    keys = _get_keys()
    result = n
    while True:
        result = _inner_decrypt(result, keys)
        if result < DOMAIN:
            return result


def encode_base62(n: int) -> str:
    """Encode n (must be < DOMAIN) as a 7-char base-62 string."""
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    digits = []
    x = n
    while x:
        digits.append(BASE62[x % 62])
        x //= 62
    s = "".join(reversed(digits)) if digits else BASE62[0]
    return s.zfill(CODE_LENGTH)


def decode_base62(s: str) -> int:
    """Decode a base-62 string back to an integer."""
    n = 0
    for ch in s:
        n = n * 62 + BASE62.index(ch)
    return n


def id_to_code(seq_id: int) -> str:
    """Convert a sequence ID to a 7-char base62 code via Feistel shuffle."""
    shuffled = _feistel_encrypt(seq_id)
    return encode_base62(shuffled)


def code_to_id(code: str) -> int:
    """Reverse: code -> seq_id. Useful for debugging only."""
    return _feistel_decrypt(decode_base62(code))


class BlockAllocator:
    """
    Leases blocks of IDs from a Postgres sequence.
    Thread-safe (within one event loop) via asyncio.Lock.
    Each process gets its own non-overlapping block; the sequence
    advances by id_block_size on each nextval() call.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._current = 0
        self._end = 0
        self._pool = None  # asyncpg pool, set by lifespan

    def set_pool(self, pool) -> None:
        self._pool = pool

    async def next_id(self) -> int:
        async with self._lock:
            if self._current >= self._end:
                await self._refill()
            val = self._current
            self._current += 1
            return val

    async def _refill(self) -> None:
        block = settings.id_block_size
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT nextval('url_id_seq') AS v")
        last = int(row["v"])
        # nextval returns the last value of the block we own
        first = last - block + 1
        self._current = first
        self._end = last + 1  # exclusive


allocator = BlockAllocator()
