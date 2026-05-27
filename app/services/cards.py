"""Layer 2 — 卡片渲染 + 状态机 (FR-006/007/010/019/021)。

US1 阶段：
- render_firing(alert) → firing 卡片 (severity 颜色 + 服务 + 时间-tz + summary, 无按钮 / 无 @)
- render_resolved(alert) → resolved 卡片 (绿色 header)
- handle_firing(...) → 渲染 + clients.lark.post_card + INSERT alerts
- handle_resolved(...) → SELECT alert + clients.lark.patch_card + UPDATE state

US2 会扩 render_firing 支持 oncall_target；US3 加 render_silenced + 6 按钮。
"""

from __future__ import annotations

import hashlib
import json
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


# ── 卡片字段取值规则（默认 — 后续可在配置层暴露 key 让运维改） ──────────────
# - 集群：labels["cluster"]
# - 环境：labels["env"]
# - 告警规则名（标题用）：labels["alertname"]
# - 告警对象：labels["instance"] → labels["host"] → labels["target"]
# - 故障描述：annotations["description"]（fallback annotations["summary"] / Incident.summary）
# - 处理手册：annotations["runbook_url"]（fallback cfg.cards.links.runbook_default_url）
# - 查看监控：annotations["__generator_url"]（由 alertmanager_inbound.alert_to_event 注入），
#   经 cfg.cards.links.generator_url_rewrites 前缀重写后给出
# - INC#：fingerprint 的 SHA1 前 6 位大写（跨重启稳定、短）
_CLUSTER_LABEL_KEY = "cluster"
_ENV_LABEL_KEY = "env"
_ALERTNAME_LABEL_KEY = "alertname"
_TARGET_LABEL_KEYS: tuple[str, ...] = ("instance", "host", "target")
_DESCRIPTION_ANNOTATION_KEYS: tuple[str, ...] = ("description", "summary")
_RUNBOOK_ANNOTATION_KEY = "runbook_url"
_GENERATOR_URL_ANNOTATION_KEY = "__generator_url"
_FIELD_FALLBACK = "-"


def _short_inc_id(fingerprint: str) -> str:
    """fingerprint → 短 INC ID（SHA1 前 6 位大写）。

    跨进程重启稳定，且不依赖 fingerprint 本身的长度/形态。
    """
    digest = hashlib.sha1(fingerprint.encode("utf-8"), usedforsecurity=False).hexdigest()
    return digest[:6].upper()


def _pick_label(labels: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """从 labels 按优先级链取第一个非空字符串值。"""
    for key in keys:
        value = labels.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _field_row(emoji: str, label: str, value: str) -> dict[str, Any]:
    """渲染一条 emoji + 加粗标签 + 值 的字段行（schema v2 markdown 组件）。

    为什么用 markdown 而不是 div + lark_md：
      飞书互动卡片 schema 2.0 下，`<at id=…></at>` / `<person id=…></person>` 这些
      用户提及标签 **只在 tag=markdown 的组件里被渲染成蓝色 @ 卡**；在 div+lark_md
      文本元素里会被当成未知 HTML 标签丢弃，最终只显示 email 兜底文本。

      官方文档（CN 站）："富文本（Markdown）组件" 章节明确说 markdown 才是
      schema 2.0 推荐的富文本载体，含完整 markdown + 飞书扩展标签支持。

      额外好处：markdown 组件结构更扁平（无嵌套 text），后续要加图标、行间样式
      也更直接。
    """
    return {
        "tag": "markdown",
        "content": f"**{emoji} {label}**：{value}",
    }


def _pick_annotation(annotations: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """按优先级链取第一个非空字符串 annotation。"""
    for key in keys:
        value = annotations.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _rewrite_url(url: str, rewrites: list[Any]) -> str:
    """按 cfg.cards.links.generator_url_rewrites 规则做前缀替换。

    顺序匹配，第一个 from_prefix 命中即用；都没命中保持原样。安全考量：
    - 替换只覆盖前缀部分（startswith），不对 path/query 做任何 strip/decode；
    - 如果重写后的 URL 不是 https/http 起头，render 时会直接退化为纯文本展示，
      避免把异常链接渲染成可点击形态。
    """
    for rule in rewrites:
        prefix = getattr(rule, "from_prefix", None)
        target = getattr(rule, "to_prefix", None)
        if isinstance(prefix, str) and isinstance(target, str) and prefix and url.startswith(prefix):
            return target + url[len(prefix) :]
    return url


def resolve_chat_id(labels: dict[str, Any]) -> str:
    """根据 alert.labels 选定目标飞书群（FR-routing）。

    遍历 cfg.lark.routes：第一条所有 match key/value 都精确等于 labels 对应值的
    route 命中即用；都不命中走 cfg.lark.group_chat_id 兜底。

    设计取舍：
      - first-match-wins 让顺序成为优先级表达手段，避免 N-to-1 排重。
      - 精确匹配而非正则：99% 场景够用、配置错误不会变"误投递"。
      - 单目标：一条 alert 对应一个 chat_id，与 Alert.lark_message_id 1:1，
        保持 silence/resolved patch 流程不变（不需要 alert_messages 子表）。
      - labels 缺少 match 中的 key → 该 route 不命中（不会误判为 ""）。
    """
    cfg = get_config()
    for route in cfg.lark.routes:
        if all(labels.get(k) == v for k, v in route.match.items()):
            return route.chat_id
    return cfg.lark.group_chat_id


def _safe_link(text: str, url: str) -> str:
    """渲染 lark_md 链接，限制只允许 http/https；其它一律退化为纯文本，杜绝
    `javascript:` / 私有 scheme 的潜在风险，对齐安全默认策略。"""
    lower = url.strip().lower()
    if lower.startswith("https://") or lower.startswith("http://"):
        return f"[{text}]({url})"
    return url or _FIELD_FALLBACK


def _alert_title(alert: Alert, *, prefix: str) -> str:
    """统一生成 firing/silenced 标题，避免状态切换后丢失 alertname/target/service。"""
    labels = alert.labels or {}
    inc_short = _short_inc_id(alert.incident_fingerprint)
    alertname = _pick_label(labels, (_ALERTNAME_LABEL_KEY,))
    target = _pick_label(labels, _TARGET_LABEL_KEYS)

    title_parts: list[str] = [f"{prefix} INC #{inc_short}"]
    if alertname:
        title_parts.append(alertname)
    if target:
        title_parts.append(f"/ {target}")
    title_parts.append(f"- {alert.service}")
    return " ".join(title_parts)


def _alert_context_elements(
    alert: Alert, *, mention: str | None = None, include_oncall: bool = True
) -> list[dict[str, Any]]:
    """完整告警上下文字段，供 firing/silenced 复用。

    这组字段是排障上下文的最小闭环：在哪个集群/环境、哪个服务/对象、严重程度、
    触发时间、故障描述、处理手册、监控链接、处理人。状态变化（silenced/resolved）
    不应让这些上下文消失。
    """
    cfg = get_config()
    when_str = _format_time_in_team_tz(alert.created_at)
    labels = alert.labels or {}
    annotations = getattr(alert, "annotations", None) or {}
    target = _pick_label(labels, _TARGET_LABEL_KEYS)
    cluster = _pick_label(labels, (_CLUSTER_LABEL_KEY,)) or _FIELD_FALLBACK
    env = _pick_label(labels, (_ENV_LABEL_KEY,)) or _FIELD_FALLBACK
    severity_text = alert.severity.title()
    description = (
        _pick_annotation(annotations, _DESCRIPTION_ANNOTATION_KEYS) or alert.summary or "-"
    )

    runbook_raw = annotations.get(_RUNBOOK_ANNOTATION_KEY)
    runbook_url = (
        runbook_raw.strip()
        if isinstance(runbook_raw, str) and runbook_raw.strip()
        else cfg.cards.links.runbook_default_url.strip()
    )
    runbook_value = _safe_link("查看 Runbook", runbook_url) if runbook_url else _FIELD_FALLBACK

    generator_raw = annotations.get(_GENERATOR_URL_ANNOTATION_KEY)
    generator_url = (
        _rewrite_url(generator_raw.strip(), list(cfg.cards.links.generator_url_rewrites))
        if isinstance(generator_raw, str) and generator_raw.strip()
        else ""
    )
    generator_value = (
        _safe_link("在监控系统查看", generator_url) if generator_url else _FIELD_FALLBACK
    )

    elements: list[dict[str, Any]] = [
        _field_row("🧩", "集群", cluster),
        _field_row("🌐", "环境", env),
        _field_row("🔧", "服务", alert.service or _FIELD_FALLBACK),
        _field_row("🔥", "严重程度", severity_text),
        _field_row("⏰", "触发时间", when_str),
        _field_row("📍", "告警对象", target or _FIELD_FALLBACK),
        _field_row("🔍", "故障描述", description),
        _field_row("📖", "处理手册", runbook_value),
        _field_row("🔗", "查看监控", generator_value),
    ]
    if include_oncall:
        elements.append(_field_row("👨\u200d🚒", "处理人员", mention or _FIELD_FALLBACK))
    return elements


def render_firing(alert: Alert, oncall_target: OncallTarget | None = None) -> dict[str, Any]:
    """firing 卡片 payload（symbol-rich 模板，schema v2）。

    版面（与产品给的设计图严格对齐）：
      Header  ：🚨 INC #{fp_hash6} {alertname?} / {target?} - {service}
      Body    ：🧩 集群 / 🔥 严重程度 / ⏰ 触发时间 / 📍 告警对象 /
                🔍 故障描述 / 👨‍🚒 处理人员
      Action ：⏱️ 静默时间 + select_static 下拉

    缺失值（如 labels 里没 alertname / instance / cluster）→ 字段值显示 "-"，
    不让卡片出现空白结构歧义。
    """
    color = _severity_color(alert.severity)
    mention = (
        oncall_target.mention_text().strip()
        if oncall_target is not None and oncall_target.recipients
        else ""
    ) or _FIELD_FALLBACK

    elements = _alert_context_elements(alert, mention=mention)
    elements.extend(_silence_select_static(alert.incident_fingerprint))

    return {
        "schema": "2.0",
        "header": {
            "title": {
                "tag": "plain_text",
                "content": _alert_title(alert, prefix="🚨"),
            },
            "template": color,
        },
        "body": {"elements": elements},
    }


def _silence_select_static(alert_fingerprint: str) -> list[dict[str, Any]]:
    """渲染 ⏱️ 静默时间 标签 + select_static 下拉。

    设计说明：
      - 用 schema v2 标准 `select_static`（不是 overflow，三点小图标对运维不直观）。
      - action.value 必须是字符串（Lark 后端规则；object 会报 200621 直接 400），
        所以把 {kind, alert_fingerprint, duration} 用紧凑 JSON 序列化。
        回调侧 `parse_lark_action_event` 会先 json.loads 再回到旧 dict 路径。
      - element_id 在卡片内全局唯一；后续若有流式更新需求可直接定位它。
    """
    cfg = get_config()

    def _encode(payload: dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    options: list[dict[str, Any]] = [
        {
            "text": {"tag": "plain_text", "content": duration},
            "value": _encode(
                {
                    "kind": "silence",
                    "alert_fingerprint": alert_fingerprint,
                    "duration": duration,
                }
            ),
        }
        for duration in cfg.silence_buttons.fixed_durations
    ]
    if cfg.silence_buttons.enable_custom:
        options.append(
            {
                "text": {"tag": "plain_text", "content": "自定义"},
                "value": _encode(
                    {
                        "kind": "custom_open",
                        "alert_fingerprint": alert_fingerprint,
                    }
                ),
            }
        )
    return [
        {
            "tag": "markdown",
            "content": "**⏱️ 静默时间**",
        },
        {
            "tag": "select_static",
            "element_id": "silence_select",
            "placeholder": {"tag": "plain_text", "content": "选择静默时长"},
            "options": options,
        },
    ]


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
    """silenced 卡片 payload — 灰色状态 header + 完整排障上下文。

    静默是状态变化，不是上下文删除。保留 firing 卡里的所有定位信息（集群/环境/
    服务/对象/描述/runbook/监控链接），只在顶部追加静默状态、操作人和到期时间；
    同时移除静默下拉框，避免对同一张已静默卡重复操作。
    """
    expires = _format_time_in_team_tz(silence.ends_at)
    operator = operator_name or silence.created_by
    elements: list[dict[str, Any]] = [
        _field_row("🔕", "静默状态", "已静默"),
        _field_row("👤", "操作人", operator),
        _field_row("⏳", "静默到期", expires),
    ]
    elements.extend(_alert_context_elements(alert, include_oncall=False))

    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": _alert_title(alert, prefix="🔕 [SILENCED]")},
            "template": "grey",
        },
        "body": {"elements": elements},
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
    chat_id = resolve_chat_id(dict(incident.labels))

    # 先建一个未持久化的 Alert 用于渲染（lark_message_id 还不知道）。
    alert_for_render = Alert(
        incident_fingerprint=incident.fingerprint,
        service=incident.service,
        severity=incident.severity,
        summary=incident.summary,
        labels=dict(incident.labels),
        annotations=dict(incident.annotations),
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
        # 重新跑 routing：保持和当初 firing 一致的目标群，避免 fallback 卡发错地方。
        fallback_chat_id = resolve_chat_id(dict(row.labels or {}))
        fallback = render_resolved(row)
        # 在 header 标记一下，让群里看出来发生了 fallback
        fallback["header"]["title"]["content"] = "⚠️ [Original card lost] " + str(
            fallback["header"]["title"]["content"]
        )
        new_id = await lark.post_card(chat_id=fallback_chat_id, card_payload=fallback)
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
        _log.info(
            "alertmanager_silence_created",
            fingerprint=alert_fingerprint,
            alertmanager_silence_id=am_id,
            duration=duration_choice,
            created_by=created_by,
            matcher_count=len(matchers),
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
    _log.info(
        "alert_silence_persisted",
        fingerprint=alert_fingerprint,
        alertmanager_silence_id=am_id,
        silence_id=silence.id,
        message_id=alert.lark_message_id,
    )
    await lark.patch_card(
        message_id=alert.lark_message_id,
        card_payload=render_silenced(alert, silence, operator_name=email or created_by),
    )
    _log.info(
        "lark_silenced_card_patched",
        fingerprint=alert_fingerprint,
        alertmanager_silence_id=am_id,
        message_id=alert.lark_message_id,
    )
    return silence
