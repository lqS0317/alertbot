"""T027 — FlashDuty webhook 验签：happy / tampered / missing / stale。

实现侧契约（research.md §1）：
- 头：X-FD-Signature: sha256=<hex(hmac_sha256(secret, ts || '.' || body))>
       X-FD-Timestamp: <unix-seconds>
- 时间戳超过 5 分钟 → 401
- 签名/时间戳缺失 → 401
- 篡改 body → 401
- 验签失败时 0 业务副作用（无 audit row, 无 alerts row）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _sign(secret: str, body: bytes, timestamp: int) -> str:
    canonical = f"{timestamp}.{body.decode('utf-8')}".encode()
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def _load_fixture() -> bytes:
    p = Path(__file__).parents[2] / "fixtures" / "flashduty" / "incident_created.json"
    return p.read_bytes()


def test_happy_path_returns_200(
    fastapi_app_factory: Callable[..., FastAPI], fd_secret: str
) -> None:
    body = _load_fixture()
    ts = int(time.time())
    sig = "sha256=" + _sign(fd_secret, body, ts)

    def lark_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_msg_001"}})

    app = fastapi_app_factory(lark_handler=lark_handler)
    with TestClient(app) as client:
        r = client.post(
            "/webhook/fd",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-FD-Signature": sig,
                "X-FD-Timestamp": str(ts),
            },
        )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "mutator,description",
    [
        (lambda body, ts, sig, hdrs: hdrs.pop("X-FD-Signature"), "missing-signature-header"),
        (lambda body, ts, sig, hdrs: hdrs.pop("X-FD-Timestamp"), "missing-timestamp-header"),
        (
            lambda body, ts, sig, hdrs: hdrs.update({"X-FD-Signature": "sha256=deadbeef"}),
            "tampered-signature",
        ),
    ],
)
def test_signature_failures_return_401(
    fastapi_app_factory: Callable[..., FastAPI],
    fd_secret: str,
    mutator: Callable[[bytes, int, str, dict[str, str]], None],
    description: str,
) -> None:
    body = _load_fixture()
    ts = int(time.time())
    sig = "sha256=" + _sign(fd_secret, body, ts)
    headers = {
        "Content-Type": "application/json",
        "X-FD-Signature": sig,
        "X-FD-Timestamp": str(ts),
    }
    mutator(body, ts, sig, headers)

    app = fastapi_app_factory(lark_handler=lambda _: httpx.Response(500))
    with TestClient(app) as client:
        r = client.post("/webhook/fd", content=body, headers=headers)
    assert r.status_code == 401, f"{description}: expected 401, got {r.status_code}: {r.text}"


def test_tampered_body_with_valid_old_signature_returns_401(
    fastapi_app_factory: Callable[..., FastAPI], fd_secret: str
) -> None:
    """对 original body 算签名，但 POST 一个被改过的 body — 必须 401。"""
    original = _load_fixture()
    ts = int(time.time())
    sig = "sha256=" + _sign(fd_secret, original, ts)

    tampered = json.dumps(
        json.loads(original.decode("utf-8")) | {"injected": "🐍"}, ensure_ascii=False
    ).encode()

    app = fastapi_app_factory(lark_handler=lambda _: httpx.Response(500))
    with TestClient(app) as client:
        r = client.post(
            "/webhook/fd",
            content=tampered,
            headers={
                "Content-Type": "application/json",
                "X-FD-Signature": sig,
                "X-FD-Timestamp": str(ts),
            },
        )
    assert r.status_code == 401


def test_stale_timestamp_returns_401(
    fastapi_app_factory: Callable[..., FastAPI], fd_secret: str
) -> None:
    """5 分钟外的时间戳必须拒绝（防回放攻击）。"""
    body = _load_fixture()
    stale_ts = int(time.time()) - 600  # 10 分钟前
    sig = "sha256=" + _sign(fd_secret, body, stale_ts)

    app = fastapi_app_factory(lark_handler=lambda _: httpx.Response(500))
    with TestClient(app) as client:
        r = client.post(
            "/webhook/fd",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-FD-Signature": sig,
                "X-FD-Timestamp": str(stale_ts),
            },
        )
    assert r.status_code == 401
