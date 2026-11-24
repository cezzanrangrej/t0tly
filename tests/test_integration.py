from __future__ import annotations
import asyncio
import time
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app, lifespan
from app.config import settings
import app.cache as cache_module
import app.ratelimit as ratelimit_module
import app.repo as repo_module


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.mark.asyncio
async def test_integration_endpoints():
    """
    Integration tests with real Postgres and real Redis covering:
    1. Create then redirect (302/301 + Location header)
    2. Custom alias conflict returns 409
    3. Expiry returns 404
    4. Rate limiting returns 429 on exceeding limit
    5. Redis outage still redirects (falls back to DB)
    """
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            # Check ready endpoint
            ready_resp = await client.get("/ready")
            assert ready_resp.status_code == 200, f"Ready check failed: {ready_resp.text}"
            ready_data = ready_resp.json()
            assert ready_data["db"] is True
            assert ready_data["redis"] is True

            # 1. Test create then redirect
            create_resp = await client.post(
                "/shorten",
                json={"url": "https://example.com/target-page"},
                headers={"X-Real-IP": "100.64.0.1"},
            )
            assert create_resp.status_code == 201
            data = create_resp.json()
            code = data["code"]
            assert len(code) == 7
            assert data["short_url"].endswith(f"/{code}")

            # Redirect
            redir_resp = await client.get(f"/{code}", follow_redirects=False)
            assert redir_resp.status_code == settings.redirect_code
            assert redir_resp.headers["location"] == "https://example.com/target-page"

            # Check stats
            stats_resp = await client.get(f"/api/stats/{code}")
            assert stats_resp.status_code == 200

            # 2. Test custom alias and alias conflict (409)
            alias = f"test-alias-{int(time.time() * 1000) % 100000}"
            alias_resp1 = await client.post(
                "/shorten",
                json={"url": "https://example.com/alias-target", "alias": alias},
                headers={"X-Real-IP": "100.64.0.2"},
            )
            assert alias_resp1.status_code == 201
            assert alias_resp1.json()["code"] == alias

            # Duplicate alias insert must return 409 (relies on PK constraint)
            alias_resp2 = await client.post(
                "/shorten",
                json={"url": "https://example.com/another-target", "alias": alias},
                headers={"X-Real-IP": "100.64.0.2"},
            )
            assert alias_resp2.status_code == 409

            # 3. Test expiry returns 404
            exp_alias = f"exp-{int(time.time() * 1000) % 100000}"
            exp_resp = await client.post(
                "/shorten",
                json={
                    "url": "https://example.com/expired-target",
                    "alias": exp_alias,
                    "ttl_seconds": 1,
                },
                headers={"X-Real-IP": "100.64.0.3"},
            )
            assert exp_resp.status_code == 201

            # Immediately should redirect
            redir_exp = await client.get(f"/{exp_alias}", follow_redirects=False)
            assert redir_exp.status_code == settings.redirect_code

            # Wait for expiry
            await asyncio.sleep(1.2)
            # Clear local cache to simulate TTL expiry/miss
            async with cache_module._local_lock:
                cache_module._local.pop(exp_alias, None)

            redir_expired = await client.get(f"/{exp_alias}", follow_redirects=False)
            assert redir_expired.status_code == 404

            # 4. Test rate limiting on POST /shorten returns 429
            limited_ip = f"198.51.100.{int(time.time()) % 200 + 10}"
            for i in range(settings.rate_limit_creates):
                r = await client.post(
                    "/shorten",
                    json={"url": f"https://example.com/page-{i}"},
                    headers={"X-Real-IP": limited_ip},
                )
                assert r.status_code == 201, f"Expected 201 on attempt {i}, got {r.status_code}"

            # The (limit + 1)-th request must return 429
            blocked_r = await client.post(
                "/shorten",
                json={"url": "https://example.com/blocked"},
                headers={"X-Real-IP": limited_ip},
            )
            assert blocked_r.status_code == 429
            assert "Retry-After" in blocked_r.headers

            # 5. Test Redis outage fallback: if Redis is down, redirects still succeed from DB
            fallback_alias = f"fb-{int(time.time() * 1000) % 100000}"
            create_fb = await client.post(
                "/shorten",
                json={"url": "https://example.com/fallback-page", "alias": fallback_alias},
                headers={"X-Real-IP": "100.64.0.4"},
            )
            assert create_fb.status_code == 201

            # Evict from local L1 cache
            async with cache_module._local_lock:
                cache_module._local.pop(fallback_alias, None)

            # Simulate Redis outage by disabling redis client in cache
            saved_redis = cache_module._redis
            try:
                cache_module.set_redis(None)
                # Should fall back to DB and still return 302
                redir_fb = await client.get(f"/{fallback_alias}", follow_redirects=False)
                assert redir_fb.status_code == settings.redirect_code
                assert redir_fb.headers["location"] == "https://example.com/fallback-page"
            finally:
                cache_module.set_redis(saved_redis)


@pytest.mark.asyncio
async def test_unknown_code_returns_404():
    """Requesting an unregistered short code must return HTTP 404."""
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            resp = await client.get("/nonexistent999", follow_redirects=False)
            assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reserved_alias_returns_400():
    """Attempting to claim a reserved system route as custom alias must return HTTP 400."""
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            resp = await client.post(
                "/shorten",
                json={"url": "https://example.com/target", "alias": "health"},
                headers={"X-Real-IP": "100.64.0.88"},
            )
            assert resp.status_code == 400
            assert "reserved" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_analytics_queue_overflow_increments_dropped():
    """When the bounded analytics queue is full, enqueue_click drops event and increments metric."""
    import app.analytics as analytics_module
    from app.metrics import ANALYTICS_DROPPED_TOTAL

    saved_queue = analytics_module._queue
    saved_build = settings.analytics_build
    try:
        settings.analytics_build = "new"
        # Set a tiny queue capacity of 2
        analytics_module._queue = asyncio.Queue(maxsize=2)

        # Enqueue 2 items (fills queue to capacity)
        analytics_module.enqueue_click("c1", "agent", "ref")
        analytics_module.enqueue_click("c2", "agent", "ref")

        # Capture baseline metric
        before = ANALYTICS_DROPPED_TOTAL._value.get()

        # 3rd item must overflow and drop without raising exception
        analytics_module.enqueue_click("c3", "agent", "ref")

        after = ANALYTICS_DROPPED_TOTAL._value.get()
        assert after == before + 1, f"Expected dropped counter to increment by 1, got delta={after - before}"
    finally:
        analytics_module._queue = saved_queue
        settings.analytics_build = saved_build
