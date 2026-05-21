"""T084 — Custom 7h flow: open modal, then submit duration and reuse silence flow."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.clients.alertmanager import AlertmanagerClient
from app.models import Silence

from .test_us3_silence_helpers import insert_firing_alert, post_lark, silence_body


def test_custom_button_opens_lark_form_modal(
    fastapi_app_factory: Callable[..., FastAPI], monkeypatch
) -> None:
    monkeypatch.setenv("TEST_LARK_VERIFY_TOKEN", "verify-secret")
    lark_posts: list[bytes] = []

    def lark_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "cards/forms" in str(req.url):
            lark_posts.append(req.content)
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(
            200, json={"code": 0, "data": {"user": {"email": "alice@company.com"}}}
        )

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        asyncio.run(insert_firing_alert(app.state.session_factory))
        r = post_lark(client, silence_body("evt-custom-open", kind="custom_open", duration=""))
        assert r.status_code == 200, r.text

    assert len(lark_posts) == 1
    payload = json.loads(lark_posts[0])
    assert payload["alert_fingerprint"] == "fp-us3"
    assert payload["field"] == "duration"


def test_custom_7h_submission_creates_7h_silence(
    fastapi_app_factory: Callable[..., FastAPI], monkeypatch
) -> None:
    monkeypatch.setenv("TEST_LARK_VERIFY_TOKEN", "verify-secret")
    am_payloads: list[dict[str, object]] = []

    def lark_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200, json={"code": 0, "data": {"user": {"email": "alice@company.com"}}}
            )
        if req.method == "PATCH":
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404)

    def am_handler(req: httpx.Request) -> httpx.Response:
        am_payloads.append(json.loads(req.content))
        return httpx.Response(200, json={"silenceID": "am-custom-7h"})

    app = fastapi_app_factory(lark_handler=lark_handler)
    app.state.alertmanager_client = AlertmanagerClient(
        base_url="http://am.test", transport=httpx.MockTransport(am_handler)
    )

    with TestClient(app) as client:
        asyncio.run(insert_firing_alert(app.state.session_factory))
        r = post_lark(client, silence_body("evt-custom-submit-7h", duration="7h"))
        assert r.status_code == 200, r.text

        async def _read() -> Silence:
            async with app.state.session_factory() as session:
                return (await session.execute(select(Silence))).scalars().one()

        silence = asyncio.run(_read())

    assert silence.duration_choice == "7h"
    assert silence.alertmanager_silence_id == "am-custom-7h"
    starts = datetime.fromisoformat(str(am_payloads[0]["startsAt"]))
    ends = datetime.fromisoformat(str(am_payloads[0]["endsAt"]))
    assert int((ends - starts).total_seconds()) == 7 * 60 * 60
