"""T068 — duplicate Lark card.action.trigger does not create duplicate silence."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.clients.alertmanager import AlertmanagerClient
from app.models import Silence
from tests.integration.flows.test_us3_silence_helpers import (
    insert_firing_alert,
    post_lark,
    silence_body,
)


def test_same_lark_event_replayed_100x_creates_one_silence(
    fastapi_app_factory: Callable[..., FastAPI],
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_LARK_VERIFY_TOKEN", "verify-secret")
    am_calls = 0

    def lark_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200, json={"code": 0, "data": {"user": {"email": "alice@company.com"}}}
            )
        if req.method == "PATCH":
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    def am_handler(_: httpx.Request) -> httpx.Response:
        nonlocal am_calls
        am_calls += 1
        return httpx.Response(200, json={"silenceID": f"am-{am_calls}"})

    app = fastapi_app_factory(lark_handler=lark_handler)
    app.state.alertmanager_client = AlertmanagerClient(
        base_url="http://am.test", transport=httpx.MockTransport(am_handler)
    )
    body = silence_body("evt-replay")

    with TestClient(app) as client:
        asyncio.run(insert_firing_alert(app.state.session_factory))
        for _ in range(100):
            r = post_lark(client, body)
            assert r.status_code == 200

        async def _count() -> int:
            async with app.state.session_factory() as session:
                return (await session.execute(select(func.count(Silence.id)))).scalar_one()

        silence_count = asyncio.run(_count())

    assert am_calls == 1
    assert silence_count == 1
