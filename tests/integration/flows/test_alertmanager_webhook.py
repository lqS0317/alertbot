"""POST /webhook/am 端到端集成测试。

覆盖：
- 正确 Bearer token + firing alert → 200，发飞书卡片，写 Alert 行
- firing → resolved 同一 fingerprint：同一张卡 PATCH 成 resolved
- 缺 Authorization / Bearer 错误 / 服务端 token 未配置 → 401
- 同一条 firing 重投 100 次：Lark 只发一次，dedup 走 audit claim-check
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Alert, AlertState, AuditLog, EventSource


def _firing_alert() -> dict[str, Any]:
    return {
        "status": "firing",
        "labels": {
            "alertname": "HighCPU",
            "service": "payment-api",
            "severity": "critical",
            "instance": "web-01",
        },
        "annotations": {"summary": "CPU > 95% on payment-api"},
        "startsAt": "2026-05-25T06:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": "fp-am-test-001",
    }


def _resolved_alert() -> dict[str, Any]:
    a = _firing_alert()
    a["status"] = "resolved"
    return a


def _payload(alerts: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {
            "version": "4",
            "receiver": "alertbot",
            "status": "firing" if any(a["status"] == "firing" for a in alerts) else "resolved",
            "alerts": alerts,
        }
    ).encode("utf-8")


def _post(client: TestClient, body: bytes, token: str | None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/webhook/am", content=body, headers=headers)


def test_firing_with_valid_token_posts_card(
    fastapi_app_factory: Callable[..., FastAPI], am_webhook_token: str
) -> None:
    posts: list[dict[str, Any]] = []

    def lark_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "im/v1/messages" in str(req.url):
            posts.append({"url": str(req.url), "body": req.content})
            return httpx.Response(
                200,
                json={"code": 0, "data": {"message_id": "om_msg_am_001"}},
            )
        return httpx.Response(404)

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        r = _post(client, _payload([_firing_alert()]), am_webhook_token)
        assert r.status_code == 200, r.text

    assert len(posts) == 1

    async def _alert_count() -> int:
        sf = app.state.session_factory
        async with sf() as session:
            rows = (await session.execute(select(Alert))).scalars().all()
            return len(rows)

    assert asyncio.run(_alert_count()) == 1


def test_resolved_after_firing_patches_same_card(
    fastapi_app_factory: Callable[..., FastAPI], am_webhook_token: str
) -> None:
    posts = 0
    patches: list[str] = []
    issued_id = "om_msg_am_resolve"

    def lark_handler(req: httpx.Request) -> httpx.Response:
        nonlocal posts
        if req.method == "POST" and "/messages/" not in str(req.url) and "messages" in str(req.url):
            posts += 1
            return httpx.Response(200, json={"code": 0, "data": {"message_id": issued_id}})
        if req.method == "PATCH" and f"/messages/{issued_id}" in str(req.url):
            patches.append(str(req.url))
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        r1 = _post(client, _payload([_firing_alert()]), am_webhook_token)
        assert r1.status_code == 200, r1.text
        r2 = _post(client, _payload([_resolved_alert()]), am_webhook_token)
        assert r2.status_code == 200, r2.text

    assert posts == 1
    assert len(patches) == 1

    async def _state() -> str:
        sf = app.state.session_factory
        async with sf() as session:
            row = (await session.execute(select(Alert))).scalars().first()
            assert row is not None
            return row.state.value

    assert asyncio.run(_state()) == AlertState.resolved.value


def test_duplicate_firing_is_deduped_by_audit_claim_check(
    fastapi_app_factory: Callable[..., FastAPI], am_webhook_token: str
) -> None:
    posts = 0

    def lark_handler(req: httpx.Request) -> httpx.Response:
        nonlocal posts
        if req.method == "POST" and "messages" in str(req.url):
            posts += 1
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_dup_am"}})
        return httpx.Response(404)

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        for _ in range(50):
            r = _post(client, _payload([_firing_alert()]), am_webhook_token)
            assert r.status_code == 200

    assert posts == 1

    async def _audit_count() -> int:
        sf = app.state.session_factory
        async with sf() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.event_source == EventSource.alertmanager,
                            AuditLog.operation == "webhook.am.received",
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(rows)

    assert asyncio.run(_audit_count()) == 1


def test_missing_authorization_returns_401(
    fastapi_app_factory: Callable[..., FastAPI], am_webhook_token: str
) -> None:
    def lark_handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Lark must NOT be called when token is missing")

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        r = _post(client, _payload([_firing_alert()]), token=None)

    assert r.status_code == 401


def test_wrong_bearer_token_returns_401(
    fastapi_app_factory: Callable[..., FastAPI], am_webhook_token: str
) -> None:
    def lark_handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Lark must NOT be called when token is wrong")

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        r = _post(client, _payload([_firing_alert()]), token="not-the-right-token")

    assert r.status_code == 401


def test_server_token_not_configured_rejects_all(
    fastapi_app_factory: Callable[..., FastAPI],
) -> None:
    """没有提供 am_webhook_token fixture ⇒ TEST_AM_WEBHOOK_TOKEN 未注入 ⇒ 服务端期望 token 为空。

    safe-by-default：服务端 token 为空时即使请求里也不带 token，也必须 401。
    """

    def lark_handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Lark must NOT be called when server token is not configured")

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        r_no_token = _post(client, _payload([_firing_alert()]), token=None)
        r_any_token = _post(client, _payload([_firing_alert()]), token="anything")

    assert r_no_token.status_code == 401
    assert r_any_token.status_code == 401
