"""Additional coverage for FlashDuty schedule response variants."""

from __future__ import annotations

import httpx
import pytest

from app.clients.flashduty import FlashDutyClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"data": {"email": "direct@company.com"}}, "direct@company.com"),
        ({"data": {"oncall": {"email": "nested@company.com"}}}, "nested@company.com"),
        ({"data": {"users": [{"email": "first@company.com"}]}}, "first@company.com"),
        ({"data": {"users": []}}, None),
        ({"data": {}}, None),
    ],
)
async def test_read_schedule_parses_supported_response_shapes(
    payload: dict[str, object], expected: str | None
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = FlashDutyClient(
        base_url="https://fd.test",
        api_token="token",
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=0,
    )
    try:
        assert await client.read_schedule("payment-api") == expected
    finally:
        await client.aclose()
