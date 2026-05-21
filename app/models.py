"""Layer 4 — SQLAlchemy 2.0 async models for AlertBot.

本模块是分层架构的最底层：MUST NOT import from `app.clients` / `app.services` /
`app.webhooks`。允许从 `app.config` / `app.observability` 引入（横向 cross-cutting）。

每张表的 UNIQUE / CHECK 都对应 Constitution 原则 II（Idempotent & Replay-Safe）和
spec FR-005 / FR-017，所有去重 + 24h 上限均落到 DB 层。
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
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(AsyncAttrs, DeclarativeBase):
    """所有 ORM 模型的基类。"""


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

    # SQLite autoincrement 只在 INTEGER PK 上工作；PostgreSQL 用 BIGSERIAL。
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
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
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
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

    UNIQUE：
      - alertmanager_silence_id：一次按钮点击 ↔ 一条 AM silence
      - lark_event_id：Lark card.action.trigger 重放去重（FR-005）

    CHECK (ends_at - starts_at <= 24h)：FR-017 / SC-008 的 DB 层硬上限。
    SQLite 用 julianday；PostgreSQL 迁移时改成 INTERVAL（见 data-model.md）。
    """

    __tablename__ = "silences"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
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
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        # SQLite-compatible 24h CHECK；PostgreSQL Alembic 迁移会改为 ends_at - starts_at <= INTERVAL '24 hours'。
        CheckConstraint(
            "(julianday(ends_at) - julianday(starts_at)) * 24.0 <= 24.0",
            name="ck_silences_max_24h",
        ),
        Index("ix_silences_alert_fingerprint", "alert_fingerprint"),
        Index("ix_silences_state_ends_at", "state", "ends_at"),
    )


class AuditLog(Base):
    """所有入站 webhook、所有出站 API、所有按钮点击的审计 + 入站去重门 (claim-check)。

    UNIQUE (event_source, dedup_key) — claim-check 模式：
      - 入站第一步 INSERT；ON CONFLICT 立即 200 OK，不进任何业务（FR-005 + CP-II）。
      - 出站审计 dedup_key 留空，不参与去重（NULL ≠ NULL in SQL UNIQUE）。
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
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


# ---------------- AsyncSession factory (T013) ---------------------------------


def make_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """生产用 PostgreSQL，本地/测试用 SQLite。URL 由 app/config.py 注入。

    例：
      "postgresql+asyncpg://user:pw@host/db"
      "sqlite+aiosqlite:///./alertbot.db"
      "sqlite+aiosqlite:///:memory:"
    """
    return create_async_engine(database_url, future=True, echo=echo, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """`expire_on_commit=False` 是 async 必需 — 否则 commit 后访问字段会触发同步刷新。"""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
