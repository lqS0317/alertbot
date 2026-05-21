"""T028 — Lark url_verification 握手测试。

Constitution VIII / FR-004 — 这一条是 Lark 后台填回调 URL 时的握手前置条件，
必须在 5s 内回 {"challenge": <value>}，且必须在签名校验之前匹配。

回归点：handshake body **没带签名头**，所以如果 url_verification 路由不在签名步骤之前，
就会被打 401 — 测试用一个无签名的 handshake 验证这个排序。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _load_handshake() -> dict[str, str]:
    p = Path(__file__).parents[2] / "fixtures" / "lark" / "url_verification.json"
    return json.loads(p.read_text())


def test_url_verification_returns_challenge_no_signature_required(
    fastapi_app_factory: Callable[..., FastAPI],
) -> None:
    """关键回归 (CP-VIII)：handshake body 没带任何签名头，必须 200，不能 401。"""
    handshake = _load_handshake()

    app = fastapi_app_factory(lark_handler=lambda _: httpx.Response(500))
    with TestClient(app) as client:
        start = time.time()
        r = client.post(
            "/webhook/lark",
            json=handshake,
            headers={"Content-Type": "application/json"},
        )
        elapsed = time.time() - start

    assert r.status_code == 200, r.text
    assert r.json() == {"challenge": handshake["challenge"]}
    assert elapsed < 5.0, "url_verification 必须 ≤ 5s 完成（Lark 硬限）"


def test_url_verification_uses_real_challenge_value(
    fastapi_app_factory: Callable[..., FastAPI],
) -> None:
    """challenge 必须原封不动回显，不允许加工。"""
    app = fastapi_app_factory(lark_handler=lambda _: httpx.Response(500))
    with TestClient(app) as client:
        r = client.post(
            "/webhook/lark",
            json={
                "type": "url_verification",
                "challenge": "🌟exact-string-with-unicode-7777",
                "token": "irrelevant",
            },
        )
    assert r.status_code == 200
    assert r.json()["challenge"] == "🌟exact-string-with-unicode-7777"
