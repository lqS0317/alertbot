"""T031 — US1 RESOLVED flow：incident.closed 必须 PATCH 同一张卡，禁止重发。

覆盖 FR-010 / FR-021 / SC-010：
  - lark.patch_card 调用 1 次，URL 含原 message_id
  - lark.post_card 调用 0 次（不能重发新卡）
  - alerts.state 变为 resolved
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

from app.models import Alert, AlertState


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


def test_closed_patches_same_message_id_and_does_not_post_new(
    fastapi_app_factory: Callable[..., FastAPI], fd_secret: str
) -> None:
    fixtures = Path(__file__).parents[2] / "fixtures" / "flashduty"
    created = fixtures.joinpath("incident_created.json").read_bytes()
    closed = fixtures.joinpath("incident_closed.json").read_bytes()

    posts: list[dict[str, Any]] = []
    patches: list[dict[str, Any]] = []
    issued_message_id = "om_msg_resolve_test_42"

    def lark_handler(req: httpx.Request) -> httpx.Response:
        if (
            req.method == "POST"
            and "im/v1/messages" in str(req.url)
            and "/" not in str(req.url).rsplit("messages", 1)[1]
        ):
            posts.append({"url": str(req.url), "body": req.content})
            return httpx.Response(
                200,
                json={"code": 0, "data": {"message_id": issued_message_id}},
            )
        if req.method == "PATCH" and "im/v1/messages/" in str(req.url):
            patches.append({"url": str(req.url), "body": req.content})
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404, json={"code": 1, "msg": "unhandled mock route"})

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        r1 = _post(client, created, fd_secret)
        assert r1.status_code == 200, r1.text
        r2 = _post(client, closed, fd_secret)
        assert r2.status_code == 200, r2.text

        # FR-010 / SC-010：post 一次，patch 一次（in-place），无第二张卡
        assert len(posts) == 1, f"expected 1 post_card, got {len(posts)}"
        assert len(patches) == 1, f"expected 1 patch_card, got {len(patches)}"
        # PATCH URL 必须命中我们刚发的 message_id
        assert issued_message_id in patches[0]["url"]

        async def _state() -> str:
            sf = app.state.session_factory
            async with sf() as session:
                row = (await session.execute(select(Alert))).scalars().first()
                assert row is not None
                return row.state.value

        assert asyncio.run(_state()) == AlertState.resolved.value


def test_duplicate_closed_is_idempotent_no_second_patch(
    fastapi_app_factory: Callable[..., FastAPI], fd_secret: str
) -> None:
    fixtures = Path(__file__).parents[2] / "fixtures" / "flashduty"
    created = fixtures.joinpath("incident_created.json").read_bytes()
    closed = fixtures.joinpath("incident_closed.json").read_bytes()

    posts = 0
    patches = 0

    def lark_handler(req: httpx.Request) -> httpx.Response:
        nonlocal posts, patches
        if (
            req.method == "POST"
            and str(req.url).endswith("messages?receive_id_type=chat_id")
            or (
                req.method == "POST"
                and "/messages" in str(req.url)
                and "/messages/" not in str(req.url)
            )
        ):
            posts += 1
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_dup"}})
        if req.method == "PATCH":
            patches += 1
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        _post(client, created, fd_secret)
        _post(client, closed, fd_secret)
        # 重放 closed 100 遍
        for _ in range(100):
            r = _post(client, closed, fd_secret)
            assert r.status_code == 200

    assert posts == 1
    # 第一次 closed PATCH 1 次；后续 100 次必须被 audit dedup 短路 → patches 仍是 1
    assert patches == 1
