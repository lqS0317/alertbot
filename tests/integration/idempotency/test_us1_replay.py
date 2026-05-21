"""T032 — 100x 重放 incident.created：必须只产生 1 条 alerts 行 + 1 次 lark.post_card。

直接对应 SC-003。
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
from sqlalchemy import func, select

from app.models import Alert, AuditLog, EventSource


def _sign(secret: str, body: bytes, timestamp: int) -> str:
    canonical = f"{timestamp}.{body.decode('utf-8')}".encode()
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def test_100x_replay_produces_one_alert_and_one_post_card(
    fastapi_app_factory: Callable[..., FastAPI], fd_secret: str
) -> None:
    body = (
        Path(__file__).parents[2] / "fixtures" / "flashduty" / "incident_created.json"
    ).read_bytes()

    posts = 0
    patches = 0

    def lark_handler(req: httpx.Request) -> httpx.Response:
        nonlocal posts, patches
        if (
            req.method == "POST"
            and "im/v1/messages" in str(req.url)
            and "/messages/" not in str(req.url)
        ):
            posts += 1
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_replay"}})
        if req.method == "PATCH":
            patches += 1
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        # 同一份 body + 同一份签名，重复 100 次
        ts = int(time.time())
        sig = "sha256=" + _sign(fd_secret, body, ts)
        headers = {
            "Content-Type": "application/json",
            "X-FD-Signature": sig,
            "X-FD-Timestamp": str(ts),
        }
        for i in range(100):
            r = client.post("/webhook/fd", content=body, headers=headers)
            assert r.status_code == 200, f"iter {i}: {r.text}"

    assert posts == 1, f"expected exactly 1 post_card, got {posts}"
    assert patches == 0, f"expected 0 patch_card on replay, got {patches}"

    async def _counts() -> tuple[int, int]:
        sf = app.state.session_factory
        async with sf() as session:
            alerts = (await session.execute(select(func.count(Alert.id)))).scalar_one()
            inbound_audits = (
                await session.execute(
                    select(func.count(AuditLog.id))
                    .where(AuditLog.event_source == EventSource.flashduty)
                    .where(AuditLog.operation == "webhook.fd.received")
                )
            ).scalar_one()
            return alerts, inbound_audits

    n_alerts, n_inbound = asyncio.run(_counts())
    assert n_alerts == 1, f"expected 1 alerts row, got {n_alerts}"
    # 第 1 次 INSERT 成功 → 99 次 IntegrityError → audit 表也只 1 条 inbound 行
    assert n_inbound == 1, f"expected 1 inbound audit row (claim-check), got {n_inbound}"
