"""T063 — silenced card renderer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import set_config_path
from app.models import Alert, AlertState, Silence, SilenceState
from app.services.cards import render_silenced


@pytest.fixture(autouse=True)
def _config(tmp_path: Path) -> None:
    yaml = tmp_path / "cfg.yaml"
    yaml.write_text(Path(__file__).parents[3].joinpath("config", "example.yaml").read_text())
    set_config_path(yaml)


def test_render_silenced_card_shows_expiry_and_operator_without_buttons() -> None:
    now = datetime(2026, 5, 7, 8, 0, tzinfo=UTC)
    alert = Alert(
        incident_fingerprint="fp-silenced",
        service="payment-api",
        severity="critical",
        summary="CPU high",
        labels={},
        lark_message_id="om_x",
        state=AlertState.silenced,
    )
    silence = Silence(
        alertmanager_silence_id="am-1",
        lark_event_id="evt-1",
        alert_fingerprint="fp-silenced",
        matchers=[],
        created_by="alice@company.com",
        actor_lark_user_id="ou_alice",
        starts_at=now,
        ends_at=now + timedelta(minutes=30),
        duration_choice="30min",
        state=SilenceState.active,
    )

    payload = render_silenced(alert, silence, operator_name="Alice")
    flat = json.dumps(payload, ensure_ascii=False)

    assert payload["header"]["template"] == "grey"
    assert "Silenced by Alice" in flat
    assert "16:30" in flat
    assert '"tag": "button"' not in flat
