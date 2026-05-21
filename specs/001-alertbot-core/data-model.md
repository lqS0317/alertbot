# Data Model — AlertBot Core

**Date**: 2026-05-07 · **Source spec**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md)

Three SQLAlchemy 2.0 models. All ORM definitions are async-compatible. SQLite is used
for tests; PostgreSQL for staging/production. The DDL below is portable; PostgreSQL-only
features (`JSONB`, `CHECK` constraints, ENUMs as TYPE) degrade cleanly to SQLite via
SQLAlchemy's dialect dispatch.

## File: `app/models.py`

```python
"""SQLAlchemy 2.0 async models for AlertBot.

Layer 4 of the 4-layer architecture: this module MUST NOT import from
`app/clients/`, `app/services/`, or `app/webhooks/`.

每张表的 UNIQUE 约束都对应 Constitution 原则 II（Idempotent & Replay-Safe）和
spec FR-005，所有去重均落到 DB 层，不依赖应用代码。
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    """所有模型的基类。"""


class AlertState(str, enum.Enum):
    firing = "firing"
    silenced = "silenced"
    resolved = "resolved"


class SilenceState(str, enum.Enum):
    active = "active"
    expired = "expired"
    cancelled = "cancelled"


class EventSource(str, enum.Enum):
    flashduty = "flashduty"
    lark = "lark"
    alertmanager = "alertmanager"
    internal = "internal"


class AuditResult(str, enum.Enum):
    success = "success"
    failure = "failure"


class Alert(Base):
    """一个 FlashDuty incident 在 AlertBot 侧的镜像。

    incident_fingerprint UNIQUE 是 incident.created 重放的去重屏障（FR-005）。
    """

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_fingerprint: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    service: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    lark_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[AlertState] = mapped_column(
        Enum(AlertState, name="alert_state"),
        nullable=False,
        default=AlertState.firing,
    )
    oncall_target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        Index("ix_alerts_state_updated_at", "state", "updated_at"),
        Index("ix_alerts_service", "service"),
    )


class Silence(Base):
    """AlertBot 通过 /api/v2/silences 创建的一条 Alertmanager silence。

    UNIQUE 约束有两条：
      - alertmanager_silence_id：保证一次按钮点击对应一条 AM silence
      - lark_event_id：保证 Lark card.action.trigger 重放不会创建第二条 silence (FR-005)

    CHECK (ends_at - starts_at <= 24h)：FR-017 / SC-008 的 DB 层硬上限。
    """

    __tablename__ = "silences"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alertmanager_silence_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    lark_event_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    alert_fingerprint: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("alerts.incident_fingerprint", ondelete="RESTRICT"),
        nullable=False,
    )
    matchers: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_lark_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_choice: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[SilenceState] = mapped_column(
        Enum(SilenceState, name="silence_state"),
        nullable=False,
        default=SilenceState.active,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        CheckConstraint(
            "(julianday(ends_at) - julianday(starts_at)) * 24 <= 24",
            name="ck_silences_max_24h_sqlite",
        ),
        Index("ix_silences_alert_fingerprint", "alert_fingerprint"),
        Index("ix_silences_state_ends_at", "state", "ends_at"),
    )


class AuditLog(Base):
    """所有入站 webhook、所有出站 API、所有按钮点击的审计 + 入站去重门 (claim-check)。

    UNIQUE (event_source, dedup_key) WHERE dedup_key IS NOT NULL：
      - 入站第一步 INSERT；ON CONFLICT 立即 200 OK，不进任何业务逻辑（FR-005 + CP-II）。
      - 出站审计 dedup_key 留空，不参与去重。
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    event_source: Mapped[EventSource] = mapped_column(
        Enum(EventSource, name="event_source"), nullable=False
    )
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_lark_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_email_redacted: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[AuditResult] = mapped_column(
        Enum(AuditResult, name="audit_result"), nullable=False
    )
    result_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        UniqueConstraint("event_source", "dedup_key", name="uq_audit_event_source_dedup_key"),
        Index("ix_audit_trace_id", "trace_id"),
        Index("ix_audit_timestamp_utc", "timestamp_utc"),
        Index("ix_audit_operation", "operation"),
    )


# ---------- AsyncSession factory ----------------------------------------------------------

def make_engine(database_url: str):
    """生产用 PostgreSQL，本地/测试用 SQLite。URL 由 app/config.py 注入。"""
    return create_async_engine(database_url, future=True, echo=False, pool_pre_ping=True)


def make_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
```

## PostgreSQL-specific overrides (production migration)

In production we replace `JSON` with `JSONB` and add a `gen_random_uuid()` server default
for `trace_id` if not supplied. These are applied via the Alembic migration generated
from the models above. The CHECK constraint uses native interval arithmetic:

```sql
ALTER TABLE silences
  ADD CONSTRAINT ck_silences_max_24h
  CHECK (ends_at - starts_at <= INTERVAL '24 hours');
```

The plan keeps the SQLite-compatible CHECK in the model (`julianday`-based) so unit
tests run in `:memory:` SQLite; the Alembic migration overrides it for PostgreSQL.

## State machines

### Alert state

```
                  incident.created           silence button click
        [start] ──────────────────► firing ─────────────────────► silenced
                                      │                               │
                                      │  incident.closed              │  incident.closed
                                      ▼                               ▼
                                  resolved ◄───────────────────── resolved
```

`silenced → firing` is **NOT** a permitted transition. v1 has no in-card cancel-silence
affordance (FR-022, dropped). Engineers cancel from the Alertmanager UI; the AlertBot
side leaves the silence record in place until its `ends_at` passes (then a periodic
reaper marks `state=expired`).

### Silence state

```
[start] ──► active ──── ends_at passes ────► expired
              │
              └──── cancelled in AM UI ────► (state stays `active` in our DB until reaper)
```

We do not poll AM for cancellation. `state` may lag reality; this is acceptable since
AM is the SoT (FR-024).

## Constraint summary (the dedup contract)

| Table | Column(s) | Type | Purpose |
|---|---|---|---|
| `alerts` | `incident_fingerprint` | UNIQUE | FD `incident.created` replay → no duplicate Alert / card |
| `silences` | `alertmanager_silence_id` | UNIQUE | One AM silence ↔ one DB row |
| `silences` | `lark_event_id` | UNIQUE | Lark `card.action.trigger` replay → no duplicate silence |
| `silences` | (`ends_at - starts_at`) | CHECK ≤ 24 h | FR-017 / SC-008 hard cap |
| `audit_log` | (`event_source`, `dedup_key`) | UNIQUE (partial) | Inbound webhook dedup gate (claim-check) |
