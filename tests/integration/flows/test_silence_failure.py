"""T065 — Alertmanager 5xx failure leaves alert unsilenced and patches failure notice."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.clients.alertmanager import AlertmanagerClient
from app.models import Alert, AlertState, Silence

from .test_us3_silence_helpers import insert_firing_alert, post_lark, silence_body


def test_alertmanager_5xx_surfaces_failure_without_silence_row(
    fastapi_app_factory: Callable[..., FastAPI],
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_LARK_VERIFY_TOKEN", "verify-secret")
    patches = 0

    def lark_handler(req: httpx.Request) -> httpx.Response:
        nonlocal patches
        if req.method == "GET":
            return httpx.Response(
                200, json={"code": 0, "data": {"user": {"email": "alice@company.com"}}}
            )
        if req.method == "PATCH":
            patches += 1
            assert b"Silence failed" in req.content
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    def am_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    app = fastapi_app_factory(lark_handler=lark_handler)
    app.state.meta_reporter = AsyncMock()
    app.state.meta_reporter.report = AsyncMock(return_value=None)
    app.state.alertmanager_client = AlertmanagerClient(
        base_url="http://am.test", transport=httpx.MockTransport(am_handler)
    )
    app.state.alertmanager_client._backoff = AsyncMock(return_value=None)

    with TestClient(app) as client:
        asyncio.run(insert_firing_alert(app.state.session_factory))
        r = post_lark(client, silence_body("evt-fail-1"))
        assert r.status_code == 200

        async def _read() -> tuple[str, int]:
            async with app.state.session_factory() as session:
                alert = (await session.execute(select(Alert))).scalars().one()
                count = (await session.execute(select(func.count(Silence.id)))).scalar_one()
                return alert.state.value, count

        state, silence_count = asyncio.run(_read())

    assert state == AlertState.firing.value
    assert silence_count == 0
    assert patches == 1
    app.state.meta_reporter.report.assert_awaited()
