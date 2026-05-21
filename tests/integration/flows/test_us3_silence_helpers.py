"""Shared helpers for US3 silence-flow integration tests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Alert, AlertState


def sign_lark(secret: str, body: bytes, ts: int | None = None, nonce: str = "n") -> dict[str, str]:
    timestamp = str(ts or int(time.time()))
    msg = f"{timestamp}{nonce}".encode() + body
    sig = base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()
    return {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": sig,
    }


def silence_body(
    event_id: str = "evt-silence-1",
    duration: str = "30min",
    *,
    kind: str = "silence",
) -> bytes:
    return json.dumps(
        {
            "schema": "2.0",
            "header": {"event_id": event_id, "event_type": "card.action.trigger"},
            "event": {
                "operator": {"user_id": "ou_alice"},
                "action": {
                    "value": {
                        "kind": kind,
                        "alert_fingerprint": "fp-us3",
                        "duration": duration,
                    }
                },
            },
        },
        separators=(",", ":"),
    ).encode()


def post_lark(client: TestClient, body: bytes, secret: str = "verify-secret") -> httpx.Response:
    return client.post("/webhook/lark", content=body, headers=sign_lark(secret, body))


async def insert_firing_alert(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        session.add(
            Alert(
                incident_fingerprint="fp-us3",
                service="payment-api",
                severity="critical",
                summary="CPU high",
                labels={"alertname": "HighCPU", "instance": "web-01", "lark_user": "alice"},
                lark_message_id="om_us3",
                state=AlertState.firing,
                created_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
            )
        )
        await session.commit()
