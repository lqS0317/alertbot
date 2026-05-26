"""Layer 1 — POST /webhook/lark 路由。

Phase 3 (US1) 阶段：只实现 url_verification 握手分支 (FR-004 / CP-VIII)。
其它 shape (card.action.trigger / event_callback) 在 Phase 5 (US3) 加。

CP-VIII 关键约束：url_verification 必须在签名校验之前匹配 — 否则握手 body
没带签名头会被打 401，Lark 后台填回调 URL 就会失败。
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.clients.lark import (
    LarkSignatureError,
    decrypt_lark_body_if_needed,
    parse_lark_action_event,
    verify_lark_signature,
)
from app.config import get_config
from app.models import AuditResult, EventSource
from app.observability import bind_trace_id, get_trace_id, new_trace_id, redact, unbind_trace_id
from app.services import audit
from app.services.cards import handle_silence_click, parse_duration, render_silence_failed

router = APIRouter(tags=["webhook-lark"])


@router.post("/webhook/lark")
async def handle_lark_webhook(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    body = json.loads(raw_body.decode("utf-8")) if raw_body else {}

    # CP-VIII：url_verification 握手必须最优先匹配，且不校验签名（飞书后台保存
    # 回调 URL 时发的握手 body 不带签名头）。
    # 但如果应用在飞书后台开启了 "加密策略" (Encrypt Key)，飞书发来的握手 body
    # 会被加密成 {"encrypt": "..."} 格式，没有 type 字段，必须先解密才能看到
    # type=url_verification。所以这里先尝试解密，再判断 type。
    if isinstance(body, dict) and "encrypt" in body:
        cfg_for_handshake = get_config()
        encrypt_key_for_handshake = os.environ.get(cfg_for_handshake.lark.encrypt_key_env, "")
        if encrypt_key_for_handshake:
            try:
                decrypted_handshake = decrypt_lark_body_if_needed(
                    encrypt_key=encrypt_key_for_handshake, body=raw_body
                )
                body = json.loads(decrypted_handshake.decode("utf-8"))
            except (LarkSignatureError, json.JSONDecodeError):
                # 解密失败留给后面签名校验分支处理（也会 401，但起码 trace 信息一致）
                pass

    if isinstance(body, dict) and body.get("type") == "url_verification":
        challenge = body.get("challenge", "")
        return {"challenge": challenge}

    token = bind_trace_id(new_trace_id())
    try:
        cfg = get_config()
        verify_secret = os.environ.get(cfg.lark.verification_token_env, "")
        encrypt_key = os.environ.get(cfg.lark.encrypt_key_env, "")
        try:
            verify_lark_signature(
                secret=verify_secret,
                body=raw_body,
                signature_header=request.headers.get("X-Lark-Signature"),
                timestamp_header=request.headers.get("X-Lark-Request-Timestamp"),
                nonce_header=request.headers.get("X-Lark-Request-Nonce"),
            )
            decrypted_body = decrypt_lark_body_if_needed(encrypt_key=encrypt_key, body=raw_body)
        except (LarkSignatureError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "signature", "reason": str(exc)},
            ) from exc

        payload = json.loads(decrypted_body.decode("utf-8"))
        action = parse_lark_action_event(payload)

        sf = request.app.state.session_factory
        async with sf() as session:
            inserted = await audit.record(
                session,
                trace_id=get_trace_id(),
                event_source=EventSource.lark,
                dedup_key=action.event_id,
                operation="webhook.lark.received",
                payload_redacted=redact(
                    {
                        "kind": action.kind,
                        "fingerprint": action.alert_fingerprint,
                        "duration": action.duration,
                    }
                ),
                result=AuditResult.success,
                actor_lark_user_id=action.operator_user_id,
            )
            if inserted is False:
                return {"ok": True, "deduped": True}

            if action.kind == "custom_open":
                if not action.alert_fingerprint:
                    return {"ok": True, "ignored": action.kind}
                await request.app.state.lark_client.open_form_modal(
                    alert_fingerprint=action.alert_fingerprint,
                    operator_user_id=action.operator_user_id,
                )
                return {"ok": True}

            if action.kind != "silence" or not action.alert_fingerprint or not action.duration:
                return {"ok": True, "ignored": action.kind}
            try:
                parse_duration(action.duration)
            except ValueError as exc:
                from sqlalchemy import select

                from app.models import Alert

                alert = (
                    await session.execute(
                        select(Alert).where(Alert.incident_fingerprint == action.alert_fingerprint)
                    )
                ).scalar_one_or_none()
                if alert is not None:
                    msg = (
                        "Silence duration cannot exceed 24h."
                        if "24h" in str(exc)
                        else "Invalid silence duration."
                    )
                    await request.app.state.lark_client.patch_card(
                        message_id=alert.lark_message_id,
                        card_payload=render_silence_failed(alert, msg),
                    )
                raise HTTPException(status_code=400, detail={"error": "invalid_duration"}) from exc

            await handle_silence_click(
                session=session,
                lark=request.app.state.lark_client,
                alertmanager=request.app.state.alertmanager_client,
                reporter=request.app.state.meta_reporter,
                event_id=action.event_id,
                alert_fingerprint=action.alert_fingerprint,
                duration_choice=action.duration,
                operator_lark_user_id=action.operator_user_id,
            )
        return {"ok": True}
    finally:
        unbind_trace_id(token)
