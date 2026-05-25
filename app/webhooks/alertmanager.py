"""Layer 1 — POST /webhook/am 路由（入站 Alertmanager 直连）。

执行流：
  1. 校验 `Authorization: Bearer <token>` — 失败 → 401, 0 业务副作用 (CP-VII)
  2. parse Alertmanager v4 payload
  3. 逐条 alert 走 audit.record() 做 claim-check 去重 (CP-II / FR-005)
  4. 按 status 分发到 services.cards.handle_firing / handle_resolved

设计决定：
- 每条 alert 独立审计 + 独立去重；同一组 alerts 里有重复时不影响其他条目
- 复用现有 `handle_firing/handle_resolved`，所以卡片渲染、@oncall、resolved
  patch、404 fallback、meta-channel 上报这些行为完全和 FlashDuty 直推一致
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from app.clients.alertmanager_inbound import (
    TokenError,
    alert_to_event,
    dedup_key_for,
    parse_payload,
    verify_am_token,
)
from app.config import get_config
from app.models import AuditResult, EventSource
from app.observability import bind_trace_id, get_trace_id, new_trace_id, redact, unbind_trace_id
from app.services import audit
from app.services.cards import handle_firing, handle_resolved

router = APIRouter(tags=["webhook-alertmanager"])


@router.post("/webhook/am")
async def handle_alertmanager_webhook(request: Request) -> dict[str, Any]:
    token = bind_trace_id(new_trace_id())
    try:
        body = await request.body()
        cfg = get_config()
        token_env = cfg.alertmanager.webhook_token_env
        expected = os.environ.get(token_env, "") if token_env else ""

        try:
            verify_am_token(request.headers.get("Authorization"), expected)
        except TokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthorized", "reason": str(exc)},
            ) from exc

        try:
            payload = parse_payload(body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "schema", "errors": exc.errors()},
            ) from exc

        sf = request.app.state.session_factory
        results: list[dict[str, Any]] = []

        for alert in payload.alerts:
            event = alert_to_event(alert)
            async with sf() as session:
                inserted = await audit.record(
                    session,
                    trace_id=get_trace_id(),
                    event_source=EventSource.alertmanager,
                    dedup_key=dedup_key_for(alert),
                    operation="webhook.am.received",
                    payload_redacted=redact(
                        {
                            "status": alert.status,
                            "fingerprint": event.incident.fingerprint,
                            "service": event.incident.service,
                            "severity": event.incident.severity,
                        }
                    ),
                    result=AuditResult.success,
                )
                if inserted is False:
                    results.append({"fp": event.incident.fingerprint, "deduped": True})
                    continue

                lark_client = request.app.state.lark_client
                reporter = request.app.state.meta_reporter

                if alert.status == "firing":
                    await handle_firing(
                        session=session,
                        lark=lark_client,
                        event=event,
                        oncall_resolver=getattr(request.app.state, "oncall_resolver", None),
                    )
                else:
                    await handle_resolved(
                        session=session,
                        lark=lark_client,
                        reporter=reporter,
                        event=event,
                    )
                results.append({"fp": event.incident.fingerprint, "ok": True})

        return {"ok": True, "alerts": results}
    finally:
        unbind_trace_id(token)
