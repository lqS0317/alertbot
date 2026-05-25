"""Layer 2 — 卡片渲染 + 状态机 (FR-006/007/010/019/021)。

US1 阶段：
- render_firing(alert) → firing 卡片 (severity 颜色 + 服务 + 时间-tz + summary, 无按钮 / 无 @)
- render_resolved(alert) → resolved 卡片 (绿色 header)
- handle_firing(...) → 渲染 + clients.lark.post_card + INSERT alerts
- handle_resolved(...) → SELECT alert + clients.lark.patch_card + UPDATE state

US2 会扩 render_firing 支持 oncall_target；US3 加 render_silenced + 6 按钮。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.alertmanager import AlertmanagerClient, matchers_from_labels
from app.clients.flashduty import FlashDutyEvent, Incident
from app.clients.lark import LarkClient, MessageNotFoundError
from app.config import get_config
from app.models import Alert, AlertState, Silence, SilenceState
from app.observability import MetaChannelReporter, get_logger
from app.services.oncall import OncallResolver, OncallTarget

_log = get_logger("alertbot.services.cards")

RESOLVED_HEADER_COLOR = "green"
DEFAULT_FALLBACK_COLOR = "grey"
DURATION_SECONDS = {
    "5min": 5 * 60,
    "30min": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "24h": 24 * 60 * 60,
}
MAX_SILENCE = timedelta(hours=24)
_DURATION_RE = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>min|h)$")


def parse_duration(value: str) -> timedelta:
    """Parse fixed/custom silence duration. Accepts 1min..24h, units min/h only."""
    match = _DURATION_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("invalid duration format")
    amount = int(match.group("amount"))
    unit = match.group("unit")
    delta = timedelta(minutes=amount) if unit == "min" else timedelta(hours=amount)
    if delta < timedelta(minutes=1) or delta > MAX_SILENCE:
        raise ValueError("duration must be between 1min and 24h")
    return delta


def _format_time_in_team_tz(when: datetime | None) -> str:
    """把时间格式化到团队配置的时区。None → '-'."""
    if when is None:
        return "-"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    tz = ZoneInfo(get_config().timezone)
    return when.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def _severity_color(severity: str) -> str:
    """从配置读 severity → color；未配置时 fallback 灰。"""
    return get_config().severity_colors.get(severity, DEFAULT_FALLBACK_COLOR)


def render_firing(alert: Alert, oncall_target: OncallTarget | None = None) -> dict[str, Any]:
    """firing 卡片 payload：标题色 + 服务 + 时间 + summary + 可选 @oncall。

    符合 Lark interactive card v2 schema。
    """
    color = _severity_color(alert.severity)
    when_str = _format_time_in_team_tz(alert.created_at)
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**Service**\n{alert.service}",
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**Time**\n{when_str}",
                    },
                },
            ],
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**Summary**\n{alert.summary}",
            },
        },
    ]
    mention = oncall_target.mention_text().strip() if oncall_target is not None else ""
    if mention:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**On-call**\n{mention}",
                },
            }
        )
    elements.append(_silence_actions(alert.incident_fingerprint))

    return {
        "schema": "2.0",
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"🚨 [{alert.severity.upper()}] {alert.service}",
            },
            "template": color,
        },
        "body": {"elements": elements},
    }


def _silence_actions(alert_fingerprint: str) -> dict[str, Any]:
    """Render fixed/custom silence buttons for Lark card actions."""
    cfg = get_config()
    actions: list[dict[str, Any]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": duration},
            "type": "default",
            "value": {
                "kind": "silence",
                "alert_fingerprint": alert_fingerprint,
                "duration": duration,
            },
        }
        for duration in cfg.silence_buttons.fixed_durations
    ]
    if cfg.silence_buttons.enable_custom:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Custom"},
                "type": "default",
                "value": {
                    "kind": "custom_open",
                    "alert_fingerprint": alert_fingerprint,
                },
            }
        )
    return {"tag": "action", "actions": actions}


def render_resolved(alert: Alert) -> dict[str, Any]:
    """resolved 卡片 payload — 绿色 header + 原 summary + 解决时间。"""
    when_str = _format_time_in_team_tz(datetime.now(UTC))
    return {
        "schema": "2.0",
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"✅ [RESOLVED] {alert.service}",
            },
            "template": RESOLVED_HEADER_COLOR,
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**Resolved at**\n{when_str}",
                    },
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**Original summary**\n{alert.summary}",
                    },
                },
            ]
        },
    }


def render_silenced(
    alert: Alert, silence: Silence, *, operator_name: str | None = None
) -> dict[str, Any]:
    """silenced 卡片 payload — 灰色 header + 到期时间 + 操作人。"""
    expires = _format_time_in_team_tz(silence.ends_at)
    operator = operator_name or silence.created_by
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": f"🔕 [SILENCED] {alert.service}"},
            "template": "grey",
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**Silenced by {operator}**\nExpires at: {expires}",
                    },
                },
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**Summary**\n{alert.summary}"},
                },
            ]
        },
    }


def render_silence_failed(alert: Alert, reason: str) -> dict[str, Any]:
    """静默失败时的原卡提示 — 不假装进入 silenced。"""
    payload = render_firing(alert)
    payload["body"]["elements"].insert(
        0,
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**Silence failed**\n{reason}",
            },
        },
    )
    return payload


# ───────────────────────── handlers (FR-009 / FR-010 / FR-021) ────────────


async def handle_firing(
    *,
    session: AsyncSession,
    lark: LarkClient,
    event: FlashDutyEvent,
    oncall_resolver: OncallResolver | None = None,
) -> Alert:
    """incident.created 处理：渲染 → post_card → INSERT alerts。

    pre-condition：调用方已经通过 services.audit.record() 拿到 True（首次到达）。
    所以这里直接做业务，不再做去重判断。
    """
    incident: Incident = event.incident
    cfg = get_config()
    chat_id = cfg.lark.group_chat_id

    # 先建一个未持久化的 Alert 用于渲染（lark_message_id 还不知道）。
    alert_for_render = Alert(
        incident_fingerprint=incident.fingerprint,
        service=incident.service,
        severity=incident.severity,
        summary=incident.summary,
        labels=dict(incident.labels),
        lark_message_id="<pending>",
        state=AlertState.firing,
        created_at=incident.started_at or datetime.now(UTC),
    )
    oncall_target = (
        await oncall_resolver.resolve(alert_for_render) if oncall_resolver is not None else None
    )
    if oncall_target is not None:
        alert_for_render.oncall_target = oncall_target.mention_text()
    payload = render_firing(alert_for_render, oncall_target=oncall_target)
    message_id = await lark.post_card(chat_id=chat_id, card_payload=payload)

    # 把真 message_id 填进去再持久化。
    alert_for_render.lark_message_id = message_id
    session.add(alert_for_render)
    await session.commit()
    _log.info(
        "alert_firing_persisted",
        fingerprint=incident.fingerprint,
        message_id=message_id,
    )
    return alert_for_render


async def handle_resolved(
    *,
    session: AsyncSession,
    lark: LarkClient,
    reporter: MetaChannelReporter,
    event: FlashDutyEvent,
) -> Alert | None:
    """incident.closed 处理：SELECT alert → patch_card → UPDATE state.

    找不到对应 alert（极罕见 — 收到 closed 但没收到 created）→ meta-channel 报告并返 None.
    PATCH 收到 404（FR-011）→ post 一张 [Original card lost] 卡 + meta-channel.
    """
    incident: Incident = event.incident
    row = (
        await session.execute(
            select(Alert).where(Alert.incident_fingerprint == incident.fingerprint)
        )
    ).scalar_one_or_none()

    if row is None:
        await reporter.report(
            "incident.closed for unknown fingerprint",
            details={"fingerprint": incident.fingerprint},
        )
        return None

    payload = render_resolved(row)
    try:
        await lark.patch_card(message_id=row.lark_message_id, card_payload=payload)
    except MessageNotFoundError:
        # FR-011 fallback：原卡丢了 → 发新卡 + 通知 meta-channel。
        cfg = get_config()
        fallback = render_resolved(row)
        # 在 header 标记一下，让群里看出来发生了 fallback
        fallback["header"]["title"]["content"] = "⚠️ [Original card lost] " + str(
            fallback["header"]["title"]["content"]
        )
        new_id = await lark.post_card(chat_id=cfg.lark.group_chat_id, card_payload=fallback)
        await reporter.report(
            "lark_message_not_found_fallback_card_posted",
            details={
                "fingerprint": incident.fingerprint,
                "lost_message_id": row.lark_message_id,
                "new_message_id": new_id,
            },
        )
        row.lark_message_id = new_id

    row.state = AlertState.resolved
    await session.commit()
    _log.info("alert_resolved", fingerprint=incident.fingerprint)
    return row


async def handle_silence_click(
    *,
    session: AsyncSession,
    lark: LarkClient,
    alertmanager: AlertmanagerClient,
    reporter: MetaChannelReporter,
    event_id: str,
    alert_fingerprint: str,
    duration_choice: str,
    operator_lark_user_id: str,
) -> Silence | None:
    """Create AM silence and patch the same card to silenced state."""
    alert = (
        await session.execute(select(Alert).where(Alert.incident_fingerprint == alert_fingerprint))
    ).scalar_one_or_none()
    if alert is None:
        await reporter.report(
            "silence_click_for_unknown_alert", details={"fingerprint": alert_fingerprint}
        )
        return None
    starts = datetime.now(UTC)
    delta = parse_duration(duration_choice)
    ends = starts + delta

    email = await lark.lookup_user_email(operator_lark_user_id)
    created_by = email or f"lark:{operator_lark_user_id}"
    if email is None:
        await reporter.report(
            "lark_user_email_missing",
            details={"user_id": operator_lark_user_id, "fingerprint": alert_fingerprint},
        )

    matchers = matchers_from_labels(alert.labels)
    try:
        am_id = await alertmanager.create_silence(
            matchers=matchers,
            starts_at=starts,
            ends_at=ends,
            created_by=created_by,
            comment=f"Silenced from Lark by {created_by}",
        )
    except Exception as exc:
        await lark.patch_card(
            message_id=alert.lark_message_id,
            card_payload=render_silence_failed(
                alert, "Alertmanager unreachable. Please retry or use the AM UI."
            ),
        )
        await reporter.report(
            "alertmanager_silence_create_failed",
            details={
                "fingerprint": alert_fingerprint,
                "error": str(exc),
                "type": type(exc).__name__,
            },
        )
        return None

    silence = Silence(
        alertmanager_silence_id=am_id,
        lark_event_id=event_id,
        alert_fingerprint=alert_fingerprint,
        matchers=matchers,
        created_by=created_by,
        actor_lark_user_id=operator_lark_user_id,
        starts_at=starts,
        ends_at=ends,
        duration_choice=duration_choice,
        state=SilenceState.active,
    )
    session.add(silence)
    alert.state = AlertState.silenced
    await session.commit()
    await lark.patch_card(
        message_id=alert.lark_message_id,
        card_payload=render_silenced(alert, silence, operator_name=email or created_by),
    )
    return silence
