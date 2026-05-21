"""FR-011 — Lark message_id 丢失时的 fallback：发新卡 + meta-channel 报告。

构造一个 Lark MockTransport：post_card 200，patch_card 总是 404。
incident.created → 卡片 OK；incident.closed → patch_card 404 → 走 fallback：
  - 重发一张以 "[Original card lost]" 开头的卡
  - meta-reporter 收到一条 "lark_message_not_found_fallback_card_posted" 报告
  - alerts.lark_message_id 被替换为 fallback 卡的 message_id
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Alert
from app.observability import MetaChannelReporter


def _sign(secret: str, body: bytes, timestamp: int) -> str:
    canonical = f"{timestamp}.{body.decode('utf-8')}".encode()
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def _post(client: TestClient, body: bytes, secret: str) -> httpx.Response:
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


def test_message_not_found_triggers_fallback_card_and_meta_report(
    fastapi_app_factory: Callable[..., FastAPI], fd_secret: str
) -> None:
    fixtures = Path(__file__).parents[2] / "fixtures" / "flashduty"
    created = fixtures.joinpath("incident_created.json").read_bytes()
    closed = fixtures.joinpath("incident_closed.json").read_bytes()

    posts: list[bytes] = []
    issued_ids = ["om_msg_first", "om_msg_fallback"]

    def lark_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/messages?" in str(req.url):
            mid = issued_ids[len(posts)]
            posts.append(req.content)
            return httpx.Response(200, json={"code": 0, "data": {"message_id": mid}})
        if req.method == "PATCH":
            return httpx.Response(404, json={"code": 1, "msg": "message not found"})
        return httpx.Response(404)

    app = fastapi_app_factory(lark_handler=lark_handler)

    # 注入一个会记录的 reporter，绕过默认的 no-op
    reports: list[tuple[str, dict[str, object]]] = []

    async def _capture(**kwargs: object) -> None:
        body = kwargs.get("body", {})
        if isinstance(body, dict):
            reports.append((str(body.get("message", "")), body))

    app.state.meta_reporter = MetaChannelReporter(post_fn=_capture)

    with TestClient(app) as client:
        r1 = _post(client, created, fd_secret)
        assert r1.status_code == 200
        r2 = _post(client, closed, fd_secret)
        assert r2.status_code == 200

    # 共 2 次 POST：第 1 次是 firing 卡，第 2 次是 fallback 卡
    assert len(posts) == 2

    # alerts 行的 lark_message_id 已经更新为 fallback 卡的 ID
    async def _read() -> str:
        sf = app.state.session_factory
        async with sf() as session:
            row = (await session.execute(select(Alert))).scalars().first()
            assert row is not None
            return row.lark_message_id

    assert asyncio.run(_read()) == "om_msg_fallback"

    # meta-channel 收到 fallback 报告
    assert any("lark_message_not_found_fallback_card_posted" in msg for msg, _ in reports), reports
