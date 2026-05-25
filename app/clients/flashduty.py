"""Layer 3 — FlashDuty client (READ-ONLY for incident state, per FR-024).

本模块只暴露：
- FlashDutyEvent / Incident: webhook payload Pydantic models
- verify_fd_signature: HMAC-SHA256 验签 + 5 分钟 replay window
- parse_event: bytes → FlashDutyEvent

US2 会再加 read_schedule（schedule API 只读）。
任何 incident-ack / incident-close / incident-snooze 接口禁止出现在本模块（FR-024）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# 5 分钟 replay window — 见 research.md §1
SIGNATURE_REPLAY_WINDOW_SECONDS = 300
# httpx 的 base_url 会把 path 整段作为前缀；这里只写相对路径，避免与
# config.flashduty.schedule_api_base 中已有的 /api/v1 双重叠加。
SCHEDULE_PATH = "/schedules"
DEFAULT_TIMEOUT_SECONDS = 5.0


class Incident(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    fingerprint: str
    service: str
    severity: str
    summary: str
    labels: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None


class FlashDutyEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event_id: str | None = None
    event_type: Literal["incident.created", "incident.updated", "incident.closed"]
    timestamp: int | None = None
    incident: Incident


class SignatureError(Exception):
    """验签失败 — 路由层应转 401 + 0 业务副作用。"""


class FlashDutyClient:
    """FlashDuty 只读客户端。

    Phase 4 只允许 schedule read。禁止 incident ack/close/snooze 等状态写操作（FR-024）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = 300,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {api_token}"} if api_token else {},
        )
        self._cache_ttl_seconds = cache_ttl_seconds
        self._now = now_fn or time.time
        self._schedule_cache: dict[str, tuple[float, str | None]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def read_schedule(self, service: str) -> str | None:
        """读取当前 service 的 on-call 邮箱，5 分钟 TTL 缓存（FR-013）。"""
        cached = self._schedule_cache.get(service)
        now = self._now()
        if cached is not None:
            expires_at, email = cached
            if now < expires_at:
                return email

        resp = await self._client.get(SCHEDULE_PATH, params={"service": service, "now": "true"})
        resp.raise_for_status()
        email = _parse_schedule_email(resp.json())
        self._schedule_cache[service] = (now + self._cache_ttl_seconds, email)
        return email


def _parse_schedule_email(payload: dict[str, Any]) -> str | None:
    """兼容几种常见 FlashDuty schedule 响应形态。"""
    data = payload.get("data")
    if isinstance(data, dict):
        direct = data.get("email")
        if isinstance(direct, str) and direct:
            return direct
        oncall = data.get("oncall")
        if isinstance(oncall, dict):
            email = oncall.get("email")
            if isinstance(email, str) and email:
                return email
        users = data.get("users")
        if isinstance(users, list) and users:
            first = users[0]
            if isinstance(first, dict):
                email = first.get("email")
                if isinstance(email, str) and email:
                    return email
    return None


def verify_fd_signature(
    *,
    secret: str,
    body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    now: int | None = None,
) -> None:
    """实现 research.md §1 的 HMAC-SHA256 over '<ts>.<body>'。

    成功无返回；失败抛 SignatureError。
    """
    if not signature_header or not timestamp_header:
        raise SignatureError("missing signature or timestamp header")

    try:
        ts = int(timestamp_header)
    except ValueError as exc:
        raise SignatureError("timestamp not an integer") from exc

    current = now if now is not None else int(time.time())
    if abs(current - ts) > SIGNATURE_REPLAY_WINDOW_SECONDS:
        raise SignatureError("timestamp outside replay window")

    if not signature_header.startswith("sha256="):
        raise SignatureError("signature must be 'sha256=<hex>' format")
    provided = signature_header[len("sha256=") :]

    canonical = f"{ts}.{body.decode('utf-8', errors='strict')}".encode()
    expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(provided, expected):
        raise SignatureError("signature mismatch")


def parse_event(body: bytes) -> FlashDutyEvent:
    """raw bytes → 已校验的 Pydantic 事件对象。失败抛 ValidationError。"""
    try:
        raw = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        # json.JSONDecodeError → 包成 Pydantic ValidationError，调用方只 catch 一种异常
        raise ValidationError.from_exception_data(
            "FlashDutyEvent",
            [
                {
                    "type": "json_invalid",
                    "loc": (),
                    "input": body,
                    "ctx": {"error": str(exc)},
                }
            ],
        ) from exc
    return FlashDutyEvent.model_validate(raw)


def dedup_key_for(event: FlashDutyEvent) -> str:
    """audit_log 的 (event_source, dedup_key) 中的 dedup_key。

    `<incident_fingerprint>:<event_type_short>` —
    incident.created / .updated / .closed 各自独立去重。
    """
    short = event.event_type.split(".", 1)[1]
    return f"{event.incident.fingerprint}:{short}"
