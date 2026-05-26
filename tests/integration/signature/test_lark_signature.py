"""T060 — Lark webhook signature + encrypted-wrapper verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _sign(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """飞书新版签名规范：SHA256((timestamp + nonce + encrypt_key).utf8() + body).hex()"""
    msg = (timestamp + nonce + secret).encode("utf-8") + body
    return hashlib.sha256(msg).hexdigest()


def _card_action_body(event_id: str = "evt-sig-1") -> bytes:
    return json.dumps(
        {
            "schema": "2.0",
            "header": {"event_id": event_id, "event_type": "card.action.trigger"},
            "event": {
                "operator": {"user_id": "ou_alice"},
                "action": {
                    "value": {
                        "kind": "silence",
                        "alert_fingerprint": "fp-x",
                        "duration": "30min",
                    }
                },
            },
        },
        separators=(",", ":"),
    ).encode()


def _headers(secret: str, body: bytes, ts: int | None = None, nonce: str = "n") -> dict[str, str]:
    timestamp = str(ts or int(time.time()))
    return {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": _sign(secret, timestamp, nonce, body),
    }


def _encrypt(encrypt_key: str, body: bytes) -> bytes:
    """飞书 Encrypt Key 加密规范：随机 16 字节 IV 拼在密文前。

    历史 bug 测试里用了 key[:16] 作 IV，刚好"自洽地"测过了同样错的解密实现；
    修复 decrypt 之后，加密辅助也要对齐规范，否则握手永远跑不通。
    """
    import os as _os

    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = _os.urandom(16)
    pad = 16 - (len(body) % 16)
    padded = body + bytes([pad]) * pad
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor().update(padded) + cipher.encryptor().finalize()
    return json.dumps(
        {"encrypt": base64.b64encode(iv + enc).decode()}, separators=(",", ":")
    ).encode()


def test_lark_signature_happy_path_reaches_handler(
    fastapi_app_factory: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 飞书新版签名 secret 是 ENCRYPT_KEY，不是 VERIFY_TOKEN（旧版用法）。
    monkeypatch.setenv("TEST_LARK_VERIFY_TOKEN", "verify-secret")
    monkeypatch.setenv("TEST_LARK_ENCRYPT_KEY", "encrypt-key")
    body = _card_action_body()
    app = fastapi_app_factory(lark_handler=lambda _: httpx.Response(200))
    with TestClient(app) as client:
        r = client.post("/webhook/lark", content=body, headers=_headers("encrypt-key", body))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("case", ["missing", "tampered", "stale"])
def test_lark_signature_failures_return_401(
    fastapi_app_factory: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    monkeypatch.setenv("TEST_LARK_VERIFY_TOKEN", "verify-secret")
    monkeypatch.setenv("TEST_LARK_ENCRYPT_KEY", "encrypt-key")
    body = _card_action_body()
    headers = _headers("encrypt-key", body)
    if case == "missing":
        headers.pop("X-Lark-Signature")
    elif case == "tampered":
        headers["X-Lark-Signature"] = "bad"
    elif case == "stale":
        headers = _headers("encrypt-key", body, ts=int(time.time()) - 600)

    app = fastapi_app_factory(lark_handler=lambda _: httpx.Response(200))
    with TestClient(app) as client:
        r = client.post("/webhook/lark", content=body, headers=headers)
    assert r.status_code == 401


def test_lark_encrypted_payload_happy_path(
    fastapi_app_factory: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LARK_VERIFY_TOKEN", "verify-secret")
    monkeypatch.setenv("TEST_LARK_ENCRYPT_KEY", "encrypt-key")
    encrypted_body = _encrypt("encrypt-key", _card_action_body("evt-encrypted"))

    app = fastapi_app_factory(lark_handler=lambda _: httpx.Response(200))
    with TestClient(app) as client:
        r = client.post(
            "/webhook/lark",
            content=encrypted_body,
            headers=_headers("encrypt-key", encrypted_body),
        )
    assert r.status_code == 200, r.text
