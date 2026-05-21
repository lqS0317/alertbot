"""T064 — silence duration DB hard cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, AlertState, Silence, SilenceState


@pytest.mark.asyncio
async def test_silence_longer_than_24h_fails_db_check(db_session: AsyncSession) -> None:
    db_session.add(
        Alert(
            incident_fingerprint="fp-long",
            service="payment-api",
            severity="critical",
            summary="CPU high",
            labels={},
            lark_message_id="om_x",
            state=AlertState.firing,
        )
    )
    await db_session.commit()

    starts = datetime(2026, 5, 7, 8, 0, tzinfo=UTC)
    db_session.add(
        Silence(
            alertmanager_silence_id="am-long",
            lark_event_id="evt-long",
            alert_fingerprint="fp-long",
            matchers=[],
            created_by="alice@company.com",
            actor_lark_user_id="ou_alice",
            starts_at=starts,
            ends_at=starts + timedelta(hours=25),
            duration_choice="25h",
            state=SilenceState.active,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()
