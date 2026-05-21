"""T007 — DB-level UNIQUE / CHECK 约束回归测试。

覆盖 spec FR-005（dedup 必须在 DB 层）+ FR-017 / SC-008（24h 硬上限）。

每个测试都直接对 SQLite 写一条违例记录，断言抛 IntegrityError。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, AlertState, AuditLog, AuditResult, EventSource, Silence, SilenceState


def _utc_now() -> datetime:
    return datetime.now(UTC)


@pytest.mark.asyncio
async def test_alerts_incident_fingerprint_unique(db_session: AsyncSession) -> None:
    """alerts.incident_fingerprint 必须 UNIQUE（FR-005 → 防 incident.created 重放产生第二条）。"""
    fp = "alertname=HighCPU,instance=web-01"
    db_session.add(_make_alert(fp))
    await db_session.commit()

    db_session.add(_make_alert(fp))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_silences_alertmanager_silence_id_unique(db_session: AsyncSession) -> None:
    """silences.alertmanager_silence_id 必须 UNIQUE（保证一次 click → 一条 AM silence）。"""
    fp = "fp-1"
    db_session.add(_make_alert(fp))
    await db_session.commit()

    db_session.add(_make_silence(am_id="am-id-1", lark_evt="evt-1", fp=fp))
    await db_session.commit()

    db_session.add(_make_silence(am_id="am-id-1", lark_evt="evt-2", fp=fp))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_silences_lark_event_id_unique(db_session: AsyncSession) -> None:
    """silences.lark_event_id 必须 UNIQUE（FR-005 → Lark card.action.trigger 重放去重）。"""
    fp = "fp-2"
    db_session.add(_make_alert(fp))
    await db_session.commit()

    db_session.add(_make_silence(am_id="am-A", lark_evt="evt-shared", fp=fp))
    await db_session.commit()

    db_session.add(_make_silence(am_id="am-B", lark_evt="evt-shared", fp=fp))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_silences_24h_check_constraint(db_session: AsyncSession) -> None:
    """silences (ends_at - starts_at) ≤ 24h 必须由 DB CHECK 强制（FR-017 / SC-008）。"""
    fp = "fp-3"
    db_session.add(_make_alert(fp))
    await db_session.commit()

    starts = _utc_now()
    ends = starts + timedelta(hours=25)  # 超 24h
    db_session.add(
        _make_silence(am_id="am-bad", lark_evt="evt-bad", fp=fp, starts=starts, ends=ends)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_silences_24h_check_accepts_exactly_24h(db_session: AsyncSession) -> None:
    """边界：恰好 24h 必须被接受。"""
    fp = "fp-3b"
    db_session.add(_make_alert(fp))
    await db_session.commit()

    starts = _utc_now()
    ends = starts + timedelta(hours=24)
    db_session.add(
        _make_silence(am_id="am-edge", lark_evt="evt-edge", fp=fp, starts=starts, ends=ends)
    )
    await db_session.commit()  # 不应抛


@pytest.mark.asyncio
async def test_audit_log_dedup_key_unique_per_source(db_session: AsyncSession) -> None:
    """audit_log (event_source, dedup_key) UNIQUE — claim-check 模式（FR-005）。"""
    db_session.add(_make_audit("flashduty", "fp-9:created"))
    await db_session.commit()

    db_session.add(_make_audit("flashduty", "fp-9:created"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_audit_log_same_dedup_different_source_allowed(db_session: AsyncSession) -> None:
    """跨 source 同 dedup_key 不应冲突（lark.event_id 与 fd.fingerprint 命名空间隔离）。"""
    db_session.add(_make_audit("flashduty", "shared-key"))
    await db_session.commit()
    db_session.add(_make_audit("lark", "shared-key"))
    await db_session.commit()  # 必须成功


@pytest.mark.asyncio
async def test_audit_log_null_dedup_key_does_not_collide(db_session: AsyncSession) -> None:
    """dedup_key NULL（出站审计）不应触发 UNIQUE 冲突。"""
    db_session.add(_make_audit("alertmanager", None, op="alertmanager.silence.create"))
    db_session.add(_make_audit("alertmanager", None, op="alertmanager.silence.create"))
    await db_session.commit()  # 两条 NULL dedup_key 必须共存


# ───────────────────────── helpers ─────────────────────────


def _make_alert(fingerprint: str) -> Alert:
    return Alert(
        incident_fingerprint=fingerprint,
        service="payment-api",
        severity="critical",
        summary="synthetic test alert",
        labels={"alertname": "HighCPU"},
        lark_message_id="msg-001",
        state=AlertState.firing,
    )


def _make_silence(
    *,
    am_id: str,
    lark_evt: str,
    fp: str,
    starts: datetime | None = None,
    ends: datetime | None = None,
) -> Silence:
    starts = starts or _utc_now()
    ends = ends or (starts + timedelta(minutes=30))
    return Silence(
        alertmanager_silence_id=am_id,
        lark_event_id=lark_evt,
        alert_fingerprint=fp,
        matchers=[{"name": "alertname", "value": "HighCPU", "isRegex": False, "isEqual": True}],
        created_by="alice@company.com",
        actor_lark_user_id="ou_alice",
        starts_at=starts,
        ends_at=ends,
        duration_choice="30min",
        state=SilenceState.active,
    )


def _make_audit(source: str, dedup_key: str | None, op: str = "webhook.fd.received") -> AuditLog:
    return AuditLog(
        trace_id="trace-test",
        event_source=EventSource(source),
        dedup_key=dedup_key,
        operation=op,
        payload_redacted={},
        result=AuditResult.success,
    )
