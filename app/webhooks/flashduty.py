"""Layer 1 — POST /webhook/fd 路由。

执行流（按 contracts/inbound-flashduty.md）：
  1. 读 X-FD-Signature / X-FD-Timestamp
  2. verify (HMAC + replay window) — 失败 → 401, 0 业务副作用 (CP-VII)
  3. parse Pydantic event
  4. 用 audit.record() 做 claim-check 去重 — 重放 → 200 OK, 立即返回 (CP-II / FR-005)
  5. dispatch by event_type → services.cards.handle_firing / handle_resolved
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from app.clients.flashduty import (
    SignatureError,
    dedup_key_for,
    parse_event,
    verify_fd_signature,
)
from app.config import get_config
from app.models import AuditResult, EventSource
from app.observability import bind_trace_id, get_trace_id, new_trace_id, redact, unbind_trace_id
from app.services import audit
from app.services.cards import handle_firing, handle_resolved

router = APIRouter(tags=["webhook-flashduty"])


@router.post("/webhook/fd")
async def handle_flashduty_webhook(request: Request) -> dict[str, Any]:
    token = bind_trace_id(new_trace_id())
    try:
        body = await request.body()
        cfg = get_config()
        secret = os.environ.get(cfg.flashduty.webhook_secret_env, "")

        # CP-VII：验签必须先做、必须返 401，0 业务副作用。
        try:
            verify_fd_signature(
                secret=secret,
                body=body,
                signature_header=request.headers.get("X-FD-Signature"),
                timestamp_header=request.headers.get("X-FD-Timestamp"),
            )
        except SignatureError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "signature", "reason": str(exc)},
            ) from exc

        try:
            event = parse_event(body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "schema", "errors": exc.errors()},
            ) from exc

        # CP-II / FR-005：claim-check 去重门
        sf = request.app.state.session_factory
        async with sf() as session:
            inserted = await audit.record(
                session,
                trace_id=get_trace_id(),
                event_source=EventSource.flashduty,
                dedup_key=dedup_key_for(event),
                operation="webhook.fd.received",
                payload_redacted=redact(
                    {
                        "event_type": event.event_type,
                        "fingerprint": event.incident.fingerprint,
                        "service": event.incident.service,
                        "severity": event.incident.severity,
                    }
                ),
                result=AuditResult.success,
            )
            if inserted is False:
                # 真重放 — 直接 200 OK，不进任何业务。
                return {"ok": True, "deduped": True}
            # inserted is None：审计写失败但不阻塞主流程（FR-026），继续业务。

            lark_client = request.app.state.lark_client
            reporter = request.app.state.meta_reporter

            if event.event_type == "incident.created":
                await handle_firing(
                    session=session,
                    lark=lark_client,
                    event=event,
                    oncall_resolver=getattr(request.app.state, "oncall_resolver", None),
                )
            elif event.event_type == "incident.closed":
                await handle_resolved(
                    session=session, lark=lark_client, reporter=reporter, event=event
                )
            elif event.event_type == "incident.updated":
                # US1 暂不处理 severity 变更，US2 会扩；先静默接受，audit 已记。
                pass

        return {"ok": True}
    finally:
        unbind_trace_id(token)
