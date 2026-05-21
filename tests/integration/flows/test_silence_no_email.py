"""T067 — missing Lark email fallback to `lark:<user_id>`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.clients.alertmanager import AlertmanagerClient
from app.models import Silence

from .test_us3_silence_helpers import insert_firing_alert, post_lark, silence_body


def test_missing_email_uses_lark_user_id_fallback_and_reports_meta(
    fastapi_app_factory: Callable[..., FastAPI],
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_LARK_VERIFY_TOKEN", "verify-secret")

    def lark_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"user": {}}})
        if req.method == "PATCH":
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    def am_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"silenceID": "am-no-email"})

    app = fastapi_app_factory(lark_handler=lark_handler)
    app.state.meta_reporter = AsyncMock()
    app.state.meta_reporter.report = AsyncMock(return_value=None)
    app.state.alertmanager_client = AlertmanagerClient(
        base_url="http://am.test", transport=httpx.MockTransport(am_handler)
    )

    with TestClient(app) as client:
        asyncio.run(insert_firing_alert(app.state.session_factory))
        r = post_lark(client, silence_body("evt-no-email"))
        assert r.status_code == 200

        async def _read() -> str:
            async with app.state.session_factory() as session:
                return (await session.execute(select(Silence.created_by))).scalar_one()

        created_by = asyncio.run(_read())

    assert created_by == "lark:ou_alice"
    app.state.meta_reporter.report.assert_awaited()
