"""Layer 2 (services) — audit write-with-dedup gateway (claim-check 模式).

入站 webhook 第一步必须调用 `record(...)`：
  - 新事件：插入 audit 行 → return True，业务继续
  - 重放（同 (event_source, dedup_key)）：UNIQUE 触发 IntegrityError → return False，
    调用方应短路返回 200（不进业务、不出站调用）
  - DB 写失败（连接断、表缺等）：return None，由调用方决定降级
    （Constitution IV / FR-026：审计写失败不阻塞主流程）
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, AuditResult, EventSource
from app.observability import get_logger

_log = get_logger("alertbot.services.audit")


async def record(
    session: AsyncSession,
    *,
    trace_id: str,
    event_source: EventSource,
    dedup_key: str | None,
    operation: str,
    payload_redacted: dict[str, Any],
    result: AuditResult,
    actor_lark_user_id: str | None = None,
    actor_email_redacted: str | None = None,
    result_summary: str | None = None,
) -> bool | None:
    """写一条审计 + claim-check 去重。

    返回值：
      - True：本次为首次写入，业务继续
      - False：UNIQUE (event_source, dedup_key) 命中，重放，业务跳过
      - None：DB 写入异常（非 UNIQUE），由调用方决定降级
    """
    row = AuditLog(
        trace_id=trace_id,
        event_source=event_source,
        dedup_key=dedup_key,
        operation=operation,
        payload_redacted=payload_redacted,
        result=result,
        actor_lark_user_id=actor_lark_user_id,
        actor_email_redacted=actor_email_redacted,
        result_summary=result_summary,
    )
    session.add(row)
    try:
        await session.commit()
        return True
    except IntegrityError:
        # UNIQUE 命中 — 真重放，预期路径，不打 warning。
        await session.rollback()
        _log.info(
            "audit_dedup_hit",
            event_source=event_source.value,
            dedup_key=dedup_key,
            operation=operation,
        )
        return False
    except SQLAlchemyError as exc:
        # 真出错（DB 不可达等）— 不阻塞主流程，记日志后由上层决定。
        await session.rollback()
        _log.error(
            "audit_write_failed",
            event_source=event_source.value,
            operation=operation,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
