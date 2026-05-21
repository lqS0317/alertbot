"""T050 — FlashDuty schedule API 5-minute cache."""

from __future__ import annotations

import httpx
import pytest

from app.clients.flashduty import FlashDutyClient


@pytest.mark.asyncio
async def test_schedule_cache_hits_within_ttl() -> None:
    calls: list[str] = []
    now = {"value": 1_000.0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        assert req.url.params["service"] == "payment-api"
        assert req.url.params["now"] == "true"
        return httpx.Response(200, json={"data": {"oncall": {"email": "bob@company.com"}}})

    client = FlashDutyClient(
        base_url="https://fd.test",
        api_token="token",
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=300,
        now_fn=lambda: now["value"],
    )
    try:
        assert await client.read_schedule("payment-api") == "bob@company.com"
        now["value"] += 120
        assert await client.read_schedule("payment-api") == "bob@company.com"
    finally:
        await client.aclose()

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_schedule_cache_expires_after_ttl() -> None:
    calls = 0
    now = {"value": 1_000.0}

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": {"email": f"bob{calls}@company.com"}})

    client = FlashDutyClient(
        base_url="https://fd.test",
        api_token="token",
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=300,
        now_fn=lambda: now["value"],
    )
    try:
        assert await client.read_schedule("payment-api") == "bob1@company.com"
        now["value"] += 301
        assert await client.read_schedule("payment-api") == "bob2@company.com"
    finally:
        await client.aclose()

    assert calls == 2
