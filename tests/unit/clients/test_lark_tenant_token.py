"""单元测试：LarkClient 自动换取 tenant_access_token 并缓存。

覆盖：
- app_id+app_secret 模式：首次请求前主动 exchange，后续 200 与 4xx 都不重复换
- 静态 tenant_token 模式（测试用）：不会去打 exchange 端点
- exchange 返回非 0 业务码 → LarkAPIError，不静默掉
- 缓存 TTL：第二次请求应直接命中已存 token，不再 exchange
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.clients.lark import (
    POST_CARD_PATH,
    TENANT_TOKEN_PATH,
    LarkAPIError,
    LarkClient,
)


def _exchange_handler_factory(
    issued_token: str = "t-fresh-001",
    expire: int = 7200,
    fail_with_code: int | None = None,
) -> tuple[Callable[[httpx.Request], httpx.Response], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": req.method,
                "path": req.url.path,
                "auth": req.headers.get("Authorization"),
                "body": req.content,
            }
        )
        if req.url.path == TENANT_TOKEN_PATH:
            if fail_with_code is not None:
                return httpx.Response(
                    200,
                    json={"code": fail_with_code, "msg": "bad credentials"},
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": issued_token,
                    "expire": expire,
                },
            )
        if req.method == "POST" and "im/v1/messages" in str(req.url):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"message_id": "om_msg_test"}},
            )
        return httpx.Response(404, json={"code": 1})

    return handler, calls


@pytest.mark.asyncio
async def test_exchange_called_once_then_cached() -> None:
    handler, calls = _exchange_handler_factory()
    transport = httpx.MockTransport(handler)
    client = LarkClient(
        app_id="cli_test",
        app_secret="secret-shared",
        transport=transport,
    )
    try:
        await client.post_card(chat_id="oc_x", card_payload={"a": 1})
        await client.post_card(chat_id="oc_x", card_payload={"a": 2})
    finally:
        await client.aclose()

    exchange_calls = [c for c in calls if c["path"] == TENANT_TOKEN_PATH]
    assert len(exchange_calls) == 1, "tenant_access_token 必须被缓存复用"
    body = json.loads(exchange_calls[0]["body"].decode("utf-8"))
    assert body == {"app_id": "cli_test", "app_secret": "secret-shared"}

    business_calls = [c for c in calls if c["path"].endswith("/im/v1/messages")]
    assert len(business_calls) == 2
    for c in business_calls:
        assert c["auth"] == "Bearer t-fresh-001"


@pytest.mark.asyncio
async def test_static_tenant_token_skips_exchange() -> None:
    handler, calls = _exchange_handler_factory()
    transport = httpx.MockTransport(handler)
    client = LarkClient(tenant_token="t-static", transport=transport)
    try:
        await client.post_card(chat_id="oc_x", card_payload={"a": 1})
    finally:
        await client.aclose()

    assert all(c["path"] != TENANT_TOKEN_PATH for c in calls)
    business = [c for c in calls if "im/v1/messages" in c["path"]]
    assert len(business) == 1
    assert business[0]["auth"] == "Bearer t-static"


@pytest.mark.asyncio
async def test_exchange_business_error_raises() -> None:
    handler, _ = _exchange_handler_factory(fail_with_code=99991663)
    transport = httpx.MockTransport(handler)
    client = LarkClient(
        app_id="cli_bad",
        app_secret="wrong",
        transport=transport,
    )
    try:
        with pytest.raises(LarkAPIError):
            await client.post_card(chat_id="oc_x", card_payload={"a": 1})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_token_refreshes_after_expiry() -> None:
    handler, calls = _exchange_handler_factory(expire=120)
    transport = httpx.MockTransport(handler)
    client = LarkClient(
        app_id="cli_test",
        app_secret="s",
        transport=transport,
    )
    try:
        await client.post_card(chat_id="oc_x", card_payload={"a": 1})
        client._tenant_token_expires_at = time.time() - 1  # 模拟过期
        await client.post_card(chat_id="oc_x", card_payload={"a": 2})
    finally:
        await client.aclose()

    exchange_calls = [c for c in calls if c["path"] == TENANT_TOKEN_PATH]
    assert len(exchange_calls) == 2, "过期后应再换一次"


@pytest.mark.asyncio
async def test_post_card_path_unchanged_after_refactor() -> None:
    """防止 _send_with_retry 重构时把请求路径丢掉。"""
    handler, calls = _exchange_handler_factory()
    transport = httpx.MockTransport(handler)
    client = LarkClient(
        app_id="cli_test",
        app_secret="s",
        transport=transport,
    )
    try:
        await client.post_card(chat_id="oc_x", card_payload={"a": 1})
    finally:
        await client.aclose()

    business = [c for c in calls if c["path"] != TENANT_TOKEN_PATH]
    assert len(business) == 1
    assert POST_CARD_PATH.split("?", 1)[0] in business[0]["path"]
