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
        """Return Lark mention syntax or role fallback text."""
        if self.kind == "user" and self.user_id is not None:
            name = self.display_name or self.email or self.user_id
            return f'<at user_id="{self.user_id}">{name}</at>'
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
                target = OncallTarget(
                    source="fallback_role",
                    recipients=tuple(
                        OncallRecipient(kind="role", role=role) for role in cfg.oncall.fallback_role
                    ),
                )
            else:  # pragma: no cover - Pydantic Literal prevents this
                target = None

            if target is not None:
                return target

        return OncallTarget(
            source="fallback_role",
            recipients=tuple(
                OncallRecipient(kind="role", role=role) for role in cfg.oncall.fallback_role
            ),
        )

    async def _from_incident_label(self, alert: Alert) -> OncallTarget | None:
        raw = alert.labels.get("lark_user")
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
