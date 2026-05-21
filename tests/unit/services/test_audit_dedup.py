"""T008 — services.audit.record() claim-check 模式回归测试。

services.audit.record(...) 是入站 webhook 的去重门：
  - 第一次写入：return True（新事件，业务继续）
  - 重复 (event_source, dedup_key)：return False（重放，业务跳过）
  - 任何其它异常 (DB 不可达等)：return None（不阻塞主流程，由调用方决定降级 — FR-026）

覆盖 spec FR-005 / FR-025 / FR-026。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, AuditResult, EventSource
from app.services import audit


async def test_audit_record_returns_true_for_new_event(db_session: AsyncSession) -> None:
    inserted = await audit.record(
        db_session,
        trace_id="trace-A",
        event_source=EventSource.flashduty,
        dedup_key="fp-1:created",
        operation="webhook.fd.received",
        payload_redacted={"summary": "x"},
        result=AuditResult.success,
    )
    assert inserted is True

    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].dedup_key == "fp-1:created"


async def test_audit_record_returns_false_on_duplicate(db_session: AsyncSession) -> None:
    """同 (event_source, dedup_key) 重放：第二次必须返 False，且 DB 仍只 1 条。"""
    common = {
        "trace_id": "trace-B",
        "event_source": EventSource.flashduty,
        "dedup_key": "fp-2:created",
        "operation": "webhook.fd.received",
        "payload_redacted": {},
        "result": AuditResult.success,
    }
    first = await audit.record(db_session, **common)
    second = await audit.record(db_session, **common)

    assert first is True
    assert second is False
    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1


async def test_audit_record_outbound_no_dedup_key_always_inserts(
    db_session: AsyncSession,
) -> None:
    """出站调用 dedup_key=None；多次写入都应成功（不参与 UNIQUE 去重）。"""
    common = {
        "trace_id": "trace-C",
        "event_source": EventSource.alertmanager,
        "dedup_key": None,
        "operation": "alertmanager.silence.create",
        "payload_redacted": {"silence_id": "am-1"},
        "result": AuditResult.success,
    }
    assert await audit.record(db_session, **common) is True
    assert await audit.record(db_session, **common) is True

    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 2
