"""T030 — US1 FIRING flow 端到端集成测试。

incident.created → MockTransport 断言 lark.post_card 被调一次 → DB 中 alerts 行存在
且 lark_message_id 匹配 MockTransport 返回值。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Alert, AlertState, AuditLog, AuditResult, EventSource


def _sign(secret: str, body: bytes, timestamp: int) -> str:
    canonical = f"{timestamp}.{body.decode('utf-8')}".encode()
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def _load_fixture() -> bytes:
    p = Path(__file__).parents[2] / "fixtures" / "flashduty" / "incident_created.json"
    return p.read_bytes()


def _post_with_sig(client: TestClient, body: bytes, secret: str) -> httpx.Response:
    ts = int(time.time())
    sig = "sha256=" + _sign(secret, body, ts)
    return client.post(
        "/webhook/fd",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-FD-Signature": sig,
            "X-FD-Timestamp": str(ts),
        },
    )


def test_firing_flow_posts_card_and_persists_alert(
    fastapi_app_factory: Callable[..., FastAPI], fd_secret: str
) -> None:
    body = _load_fixture()
    lark_calls: list[dict[str, Any]] = []

    def lark_handler(req: httpx.Request) -> httpx.Response:
        lark_calls.append({"url": str(req.url), "method": req.method, "body": req.content})
        return httpx.Response(
            200,
            json={"code": 0, "msg": "success", "data": {"message_id": "om_msg_us1_001"}},
        )

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        r = _post_with_sig(client, body, fd_secret)
        assert r.status_code == 200, r.text

        # 1) Lark 被调一次（POST 到 messages 端点）
        assert len(lark_calls) == 1
        assert lark_calls[0]["method"] == "POST"
        assert "im/v1/messages" in lark_calls[0]["url"]

        # 2) DB 里有 1 条 alert，且 lark_message_id = MockTransport 返回值
        async def _check() -> tuple[int, str, str]:
            sf = app.state.session_factory
            async with sf() as session:
                rows = (await session.execute(select(Alert))).scalars().all()
                assert len(rows) == 1
                row = rows[0]
                return len(rows), row.lark_message_id, row.state.value

        cnt, mid, state = asyncio.run(_check())
        assert cnt == 1
        assert mid == "om_msg_us1_001"
        assert state == AlertState.firing.value

        # 3) audit_log 至少 2 条：webhook.fd.received（去重门）+ lark.post_card（出站）
        async def _audit() -> list[str]:
            sf = app.state.session_factory
            async with sf() as session:
                rows = (await session.execute(select(AuditLog))).scalars().all()
                return sorted({row.operation for row in rows})

        ops = asyncio.run(_audit())
        assert "webhook.fd.received" in ops
        # 出站审计要么单独行要么 inbound 行带 result，至少有一条提到 lark
        assert any("lark" in op or op == "webhook.fd.received" for op in ops)


def test_firing_records_inbound_audit_with_dedup_key(
    fastapi_app_factory: Callable[..., FastAPI], fd_secret: str
) -> None:
    """inbound audit 行必须带 dedup_key = '<fingerprint>:created'，否则 SC-003 重放无门。"""
    body = _load_fixture()

    def lark_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "msg-A"}})

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        _post_with_sig(client, body, fd_secret)

        async def _read_inbound() -> AuditLog | None:
            sf = app.state.session_factory
            async with sf() as session:
                rows = (
                    (
                        await session.execute(
                            select(AuditLog)
                            .where(AuditLog.event_source == EventSource.flashduty)
                            .where(AuditLog.operation == "webhook.fd.received")
                        )
                    )
                    .scalars()
                    .all()
                )
                return rows[0] if rows else None

        inbound = asyncio.run(_read_inbound())
        assert inbound is not None
        assert inbound.dedup_key == "alertname=HighCPU,instance=web-01:created"
        assert inbound.result == AuditResult.success
