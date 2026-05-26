"""Layer 3 — 入站 Alertmanager webhook 解析 + 共享 token 鉴权。

Alertmanager 直接订阅 AlertBot 时使用：

  Alertmanager  --(webhook v4 + Bearer token)-->  POST /webhook/am

为了复用现有 `services.cards.handle_firing / handle_resolved`，本模块把每个
Alertmanager alert 适配成 `FlashDutyEvent` 的等价形态（`incident.created`
/ `incident.closed`），不会污染 FlashDuty 的出站客户端。
"""

from __future__ import annotations

import hmac
import json
import time
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.clients.flashduty import FlashDutyEvent, Incident


class TokenError(Exception):
    """共享 token 校验失败 — 路由层应转 401 + 0 业务副作用。"""


class AlertmanagerInboundAlert(BaseModel):
    """Alertmanager webhook v4 中单条 alert 的精简 schema。

    `extra="ignore"`：Alertmanager 还会带 generatorURL / silenceURL 等字段，
    我们不关心，忽略即可。
    """

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime | None = Field(default=None, alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    fingerprint: str | None = None
    generator_url: str | None = Field(default=None, alias="generatorURL")


class AlertmanagerInboundPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    version: str = "4"
    receiver: str = ""
    status: Literal["firing", "resolved"] | None = None
    alerts: list[AlertmanagerInboundAlert] = Field(default_factory=list)


def verify_am_token(authorization_header: str | None, expected_token: str) -> None:
    """验证 `Authorization: Bearer <token>`。

    服务端 token 为空 ⇒ 视为未配置，全拒（避免无意中开放公网入口）。
    用 `hmac.compare_digest` 做常量时间比较，避免 timing attack。
    """
    if not expected_token:
        raise TokenError("alertmanager webhook token not configured on server")
    if not authorization_header:
        raise TokenError("missing Authorization header")
    prefix = "Bearer "
    if not authorization_header.startswith(prefix):
        raise TokenError("Authorization must be 'Bearer <token>'")
    provided = authorization_header[len(prefix) :].strip()
    if not provided:
        raise TokenError("empty bearer token")
    if not hmac.compare_digest(provided, expected_token):
        raise TokenError("token mismatch")


def parse_payload(body: bytes) -> AlertmanagerInboundPayload:
    """raw bytes → 已校验的 Pydantic payload。失败抛 ValidationError。"""
    try:
        raw = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError.from_exception_data(
            "AlertmanagerInboundPayload",
            [
                {
                    "type": "json_invalid",
                    "loc": (),
                    "input": body,
                    "ctx": {"error": str(exc)},
                }
            ],
        ) from exc
    return AlertmanagerInboundPayload.model_validate(raw)


def alert_to_event(alert: AlertmanagerInboundAlert) -> FlashDutyEvent:
    """把单条 Alertmanager alert 适配成内部 FlashDutyEvent，复用下游 handler。"""
    fingerprint = alert.fingerprint or _compute_fingerprint(alert.labels)
    service = alert.labels.get("service") or alert.labels.get("alertname") or "unknown"
    severity = alert.labels.get("severity") or "info"
    summary = (
        alert.annotations.get("summary")
        or alert.annotations.get("description")
        or alert.labels.get("alertname")
        or "alert"
    )
    annotations: dict[str, Any] = dict(alert.annotations)
    if alert.generator_url:
        # generatorURL 不是 Alertmanager 的 annotation；用 __ 前缀避免和真实 annotation 冲突。
        annotations["__generator_url"] = alert.generator_url
    incident = Incident(
        fingerprint=fingerprint,
        service=service,
        severity=severity,
        summary=summary,
        labels=dict(alert.labels),
        annotations=annotations,
        started_at=alert.starts_at,
    )
    event_type: Literal["incident.created", "incident.closed"] = (
        "incident.created" if alert.status == "firing" else "incident.closed"
    )
    return FlashDutyEvent(
        event_id=f"{fingerprint}:{alert.status}",
        event_type=event_type,
        timestamp=int(time.time()),
        incident=incident,
    )


def dedup_key_for(alert: AlertmanagerInboundAlert) -> str:
    """`<fingerprint>:firing` / `<fingerprint>:resolved` — Alertmanager 重投幂等门。"""
    fingerprint = alert.fingerprint or _compute_fingerprint(alert.labels)
    return f"{fingerprint}:{alert.status}"


def _compute_fingerprint(labels: dict[str, Any]) -> str:
    """labels → 稳定 fingerprint。

    Alertmanager 0.16+ 会自带 fingerprint；只在缺失时退化成 labels 哈希，
    保证同一组 labels 在多次重投里得到同一 dedup_key。
    """
    import hashlib

    canonical = json.dumps({k: str(labels[k]) for k in sorted(labels)}, ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()
