"""LarkClient retry / error 路径单元测试 (FR-011 / FR-027)。

直接对 LarkClient 注入 MockTransport，断言：
  - 5xx 触发重试（最多 3 次）
  - PATCH 收到 404 → MessageNotFoundError
  - business code 非零 → LarkAPIError
"""

from __future__ import annotations

import httpx
import pytest

from app.clients.lark import LarkAPIError, LarkClient, MessageNotFoundError


@pytest.mark.asyncio
async def test_post_card_retries_on_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="upstream sad")
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_ok"}})

    client = LarkClient(transport=httpx.MockTransport(handler), max_retries=3)
    try:
        # 第 3 次成功 — 但本测试关心 retry 计数，不要等待 backoff
        msg_id = await _no_sleep_post_card(client)
    finally:
        await client.aclose()
    assert msg_id == "om_ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_post_card_gives_up_after_max_retries() -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, text="always-bad")

    client = LarkClient(transport=httpx.MockTransport(handler), max_retries=3)
    try:
        with pytest.raises(LarkAPIError) as exc_info:
            await _no_sleep_post_card(client)
    finally:
        await client.aclose()
    assert calls["n"] == 3
    assert exc_info.value.status_code in {502, 599}


@pytest.mark.asyncio
async def test_patch_card_404_raises_message_not_found() -> None:
    """FR-011 — caller 据此触发 'Original card lost' 兜底。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = LarkClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(MessageNotFoundError):
            await client.patch_card(message_id="om_gone", card_payload={"x": 1})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_business_code_nonzero_raises_lark_api_error() -> None:
    """Lark HTTP 200 但 body code != 0 仍然算失败。"""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 99991663, "msg": "rate limited"})

    client = LarkClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(LarkAPIError):
            await client.post_card(chat_id="oc_x", card_payload={})
    finally:
        await client.aclose()


# ───────────────────────── helpers ──────────────────────────────────


async def _no_sleep_post_card(client: LarkClient) -> str:
    """绕过 backoff sleep — 测试时把 backoff 短路掉，跑得快。"""
    import app.clients.lark as lark_mod

    real_backoff = lark_mod.LarkClient._backoff

    async def _instant(self: LarkClient, attempt: int) -> None:
        return None

    lark_mod.LarkClient._backoff = _instant  # type: ignore[method-assign]
    try:
        return await client.post_card(chat_id="oc_test", card_payload={})
    finally:
        lark_mod.LarkClient._backoff = real_backoff  # type: ignore[method-assign]
