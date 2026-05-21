"""T086 — malformed custom duration is rejected before Alertmanager call."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.clients.alertmanager import AlertmanagerClient
from app.models import Silence

from .test_us3_silence_helpers import insert_firing_alert, post_lark, silence_body


def test_malformed_custom_duration_rejected_without_am_call(
    fastapi_app_factory: Callable[..., FastAPI], monkeypatch
) -> None:
    monkeypatch.setenv("TEST_LARK_VERIFY_TOKEN", "verify-secret")
    am_calls = 0
    patches = 0

    def lark_handler(req: httpx.Request) -> httpx.Response:
        nonlocal patches
        if req.method == "GET":
            return httpx.Response(
                200, json={"code": 0, "data": {"user": {"email": "alice@company.com"}}}
            )
        if req.method == "PATCH":
            patches += 1
            assert b"Invalid silence duration" in req.content
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    def am_handler(_: httpx.Request) -> httpx.Response:
        nonlocal am_calls
        am_calls += 1
        return httpx.Response(200, json={"silenceID": "should-not-happen"})

    app = fastapi_app_factory(lark_handler=lark_handler)
    app.state.alertmanager_client = AlertmanagerClient(
        base_url="http://am.test", transport=httpx.MockTransport(am_handler)
    )

    with TestClient(app) as client:
        asyncio.run(insert_firing_alert(app.state.session_factory))
        r = post_lark(client, silence_body("evt-custom-bad", duration="banana"))
        assert r.status_code == 400

        async def _count() -> int:
            async with app.state.session_factory() as session:
                return (await session.execute(select(func.count(Silence.id)))).scalar_one()

        count = asyncio.run(_count())

    assert count == 0
    assert am_calls == 0
    assert patches == 1
