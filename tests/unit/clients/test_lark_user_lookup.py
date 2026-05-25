"""Phase 4 coverage for Lark user lookup helpers (T055/T056)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.clients.lark import LarkAPIError, LarkClient


@pytest.mark.asyncio
async def test_lookup_user_email_returns_email_and_caches() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "/contact/v3/users/ou_alice" in str(req.url)
        assert req.url.params["user_id_type"] == "open_id"
        return httpx.Response(
            200,
            json={"code": 0, "data": {"user": {"email": "alice@company.com"}}},
        )

    client = LarkClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.lookup_user_email("ou_alice") == "alice@company.com"
        assert await client.lookup_user_email("ou_alice") == "alice@company.com"
    finally:
        await client.aclose()

    assert calls == 1


@pytest.mark.asyncio
async def test_lookup_user_email_404_returns_none_and_caches() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": 404, "msg": "not found"})

    client = LarkClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.lookup_user_email("ou_missing") is None
        assert await client.lookup_user_email("ou_missing") is None
    finally:
        await client.aclose()

    assert calls == 1


@pytest.mark.asyncio
async def test_lookup_user_email_non_404_business_error_raises() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 999, "msg": "bad app token"})

    client = LarkClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(LarkAPIError):
            await client.lookup_user_email("ou_broken")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_lookup_user_by_email_parses_user_list_and_caches() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "/contact/v3/users/batch_get_id" in str(req.url)
        assert req.method == "POST"
        assert json.loads(req.content.decode("utf-8")) == {"emails": ["bob@company.com"]}
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "user_list": [
                        {
                            "email": "bob@company.com",
                            "user_id": "ou_bob",
                            "name": "Bob",
                        }
                    ]
                },
            },
        )

    client = LarkClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.lookup_user_by_email("bob@company.com") == ("ou_bob", "Bob")
        assert await client.lookup_user_by_email("bob@company.com") == ("ou_bob", "Bob")
    finally:
        await client.aclose()

    assert calls == 1


@pytest.mark.asyncio
async def test_lookup_user_by_email_parses_email_users_map() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "email_users": {
                        "carol@company.com": {
                            "open_id": "ou_carol",
                            "display_name": "Carol",
                        }
                    }
                },
            },
        )

    client = LarkClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.lookup_user_by_email("carol@company.com") == ("ou_carol", "Carol")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_lookup_user_by_email_business_error_returns_none() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 999, "msg": "bad query"})

    client = LarkClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.lookup_user_by_email("nobody@company.com") is None
    finally:
        await client.aclose()
