"""Layer 2 — D-plan on-call resolver (FR-012 / FR-013).

Priority chain:
incident.labels.lark_user → FlashDuty schedule API → static service map → fallback role.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.config import get_config
from app.models import Alert
from app.observability import MetaChannelReporter, get_logger

_log = get_logger("alertbot.services.oncall")

OncallKind = Literal["user", "role"]
OncallSource = Literal["incident_label", "fd_schedule", "static_map", "fallback_role"]


class FlashDutyScheduleReader(Protocol):
    async def read_schedule(self, service: str) -> str | None: ...


class LarkUserLookup(Protocol):
    async def lookup_user_by_email(self, email: str) -> tuple[str, str] | None: ...


@dataclass(frozen=True)
class OncallRecipient:
    kind: OncallKind
    email: str | None = None
    user_id: str | None = None
    display_name: str | None = None
    role: str | None = None

    def mention_text(self) -> str:
        """Return Lark interactive-card lark_md mention syntax or fallback text.

        飞书互动卡片 lark_md 标签里 @ 用户的正确语法（与 IM 消息体里的
        `<at user_id="…">Name</at>` **不同**）：
          - 用 open_id / user_id：`<at id=ou_xxx></at>`（属性名是 `id`，值不带引号，
            标签内必须为空 — 名字由飞书后端按 id 自动渲染）
          - 用 email（仅自建/商店应用）：`<at email=xxx@xxx.com></at>`
        其它属性名（含 `user_id="…"`）或在标签内塞 name，会被飞书当未知 HTML 直接
        丢弃，导致 "On-call" 区块下方空白。

        参考：
          https://open.feishu.cn/document/ukTMukTMukTM/uADOwUjLwgDM14CM4ATN
          https://open.feishu.cn/document/common-capabilities/message-card/message-cards-content/using-markdown-tags
        """
        if self.kind == "user":
            if self.user_id:
                return f"<at id={self.user_id}></at>"
            # 故意 NOT 使用 `<at email=…></at>`：飞书会对 email 做存在性校验，配置里拼错
            # 一个邮箱字符就会让整张卡 400 拒收（ErrCode 100290 "invalid user resource"），
            # 把告警直接打丢。降级为纯文本邮箱：飞书永远接受，运维肉眼也能看出谁配错了，
            # 且应用日志里有 lark_lookup_user_by_email_not_found warning 辅助定位。
            if self.email:
                return self.email
        if self.kind == "role" and self.role is not None:
            return self.role
        return self.email or self.role or "@on-call"


@dataclass(frozen=True)
class OncallTarget:
    source: OncallSource
    recipients: tuple[OncallRecipient, ...]

    @property
    def kind(self) -> OncallKind:
        return self.recipients[0].kind if self.recipients else "role"

    @property
    def email(self) -> str | None:
        return self.recipients[0].email if self.recipients else None

    @property
    def user_id(self) -> str | None:
        return self.recipients[0].user_id if self.recipients else None

    @property
    def display_name(self) -> str | None:
        return self.recipients[0].display_name if self.recipients else None

    @property
    def role(self) -> str | None:
        return self.recipients[0].role if self.recipients else None

    def mention_text(self) -> str:
        return " ".join(recipient.mention_text() for recipient in self.recipients)


class OncallResolver:
    def __init__(
        self,
        *,
        flashduty: FlashDutyScheduleReader,
        lark: LarkUserLookup,
        reporter: MetaChannelReporter | None = None,
    ) -> None:
        self._flashduty = flashduty
        self._lark = lark
        self._reporter = reporter

    async def resolve(self, alert: Alert) -> OncallTarget:
        """Resolve current on-call by configured D-plan priority chain."""
        cfg = get_config()
        for tier in cfg.oncall.priority_chain:
            if tier == "incident_label":
                target = await self._from_incident_label(alert)
            elif tier == "fd_schedule":
                target = await self._from_flashduty_schedule(alert)
            elif tier == "static_map":
                target = await self._from_static_map(alert)
            elif tier == "fallback_role":
                target = await self._build_fallback_target(cfg.oncall.fallback_role)
            else:  # pragma: no cover - Pydantic Literal prevents this
                target = None

            if target is not None:
                return target

        return await self._build_fallback_target(cfg.oncall.fallback_role)

    async def _build_fallback_target(self, items: list[str]) -> OncallTarget:
        """fallback_role 元素自适应：含 '@' → 当 email 走 lookup（飞书 @ 真人卡），
        否则当 role 字符串文本（如 '@on-call'）。

        让运维可以混用：fallback_role: ["sunyu@hashkey.cloud", "@on-call"]
        既保证真人被通知，又留一条角色文本兜底（lookup 失败时还能看到文字）。
        """
        recipients: list[OncallRecipient] = []
        for item in items:
            if "@" in item and "." in item.split("@", 1)[-1]:
                # 看起来像 email（user@domain.tld）→ 走 lookup 拿 user_id 渲染真 @ 卡
                recipients.append(await self._recipient_from_email(item))
            else:
                # 角色名 / 群组标签 → 当纯文本 role
                recipients.append(OncallRecipient(kind="role", role=item))
        return OncallTarget(source="fallback_role", recipients=tuple(recipients))

    async def _from_incident_label(self, alert: Alert) -> OncallTarget | None:
        raw = alert.labels.get(get_config().oncall.incident_label_key)
        if not isinstance(raw, str) or not raw:
            return None
        return await self._target_from_email(raw, source="incident_label")

    async def _from_flashduty_schedule(self, alert: Alert) -> OncallTarget | None:
        try:
            email = await self._flashduty.read_schedule(alert.service)
        except Exception as exc:
            _log.warning(
                "flashduty_schedule_lookup_failed",
                service=alert.service,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            if self._reporter is not None:
                await self._reporter.report(
                    "flashduty_schedule_lookup_failed",
                    details={
                        "service": alert.service,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
            return None
        if not email:
            return None
        return await self._target_from_email(email, source="fd_schedule")

    async def _from_static_map(self, alert: Alert) -> OncallTarget | None:
        emails = get_config().oncall.static_service_map.get(alert.service)
        if not emails:
            return None
        return await self._target_from_emails(emails, source="static_map")

    async def _target_from_email(self, email: str, *, source: OncallSource) -> OncallTarget:
        return await self._target_from_emails([email], source=source)

    async def _target_from_emails(self, emails: list[str], *, source: OncallSource) -> OncallTarget:
        recipients = []
        for email in emails:
            recipients.append(await self._recipient_from_email(email))
        return OncallTarget(source=source, recipients=tuple(recipients))

    async def _recipient_from_email(self, email: str) -> OncallRecipient:
        user = await self._lark.lookup_user_by_email(email)
        if user is None:
            # 仍然返回 user target，卡片可显示 email；后续 Phase 可接 meta-channel。
            return OncallRecipient(kind="user", email=email)
        user_id, display_name = user
        return OncallRecipient(
            kind="user",
            email=email,
            user_id=user_id,
            display_name=display_name,
        )
