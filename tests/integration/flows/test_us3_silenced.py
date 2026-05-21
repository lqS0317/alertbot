"""T070 — end-to-end SILENCED flow."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.clients.alertmanager import AlertmanagerClient
from app.models import Alert, AlertState, Silence

from .test_us3_silence_helpers import insert_firing_alert, post_lark, silence_body


def test_silence_click_creates_am_silence_and_patches_same_card(
    fastapi_app_factory: Callable[..., FastAPI],
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_LARK_VERIFY_TOKEN", "verify-secret")
    lark_requests: list[tuple[str, str, bytes]] = []
    am_requests: list[bytes] = []

    def lark_handler(req: httpx.Request) -> httpx.Response:
        lark_requests.append((req.method, str(req.url), req.content))
        if req.method == "GET":
            return httpx.Response(
                200, json={"code": 0, "data": {"user": {"email": "alice@company.com"}}}
            )
        if req.method == "PATCH":
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    def am_handler(req: httpx.Request) -> httpx.Response:
        am_requests.append(req.content)
        return httpx.Response(200, json={"silenceID": "am-us3-1"})

    app = fastapi_app_factory(lark_handler=lark_handler)
    app.state.alertmanager_client = AlertmanagerClient(
        base_url="http://am.test", transport=httpx.MockTransport(am_handler)
    )

    with TestClient(app) as client:
        asyncio.run(insert_firing_alert(app.state.session_factory))
        r = post_lark(client, silence_body("evt-us3-1"))
        assert r.status_code == 200, r.text

        async def _read() -> tuple[str, str, str]:
            async with app.state.session_factory() as session:
                alert = (await session.execute(select(Alert))).scalars().one()
                silence = (await session.execute(select(Silence))).scalars().one()
                return alert.state.value, silence.created_by, silence.alertmanager_silence_id

        state, created_by, am_id = asyncio.run(_read())

    assert state == AlertState.silenced.value
    assert created_by == "alice@company.com"
    assert am_id == "am-us3-1"
    assert len(am_requests) == 1
    am_payload = json.loads(am_requests[0])
    assert am_payload["createdBy"] == "alice@company.com"
    assert {
        "name": "alertname",
        "value": "HighCPU",
        "isRegex": False,
        "isEqual": True,
    } in am_payload["matchers"]
    patch_urls = [url for method, url, _ in lark_requests if method == "PATCH"]
    assert len(patch_urls) == 1
    assert "om_us3" in patch_urls[0]
