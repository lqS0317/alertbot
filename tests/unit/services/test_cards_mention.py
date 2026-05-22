"""T051 — firing card @-mention rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import set_config_path
from app.models import Alert, AlertState
from app.services.cards import render_firing
from app.services.oncall import OncallRecipient, OncallTarget


@pytest.fixture(autouse=True)
def _config(tmp_path: Path) -> None:
    yaml = tmp_path / "cfg.yaml"
    yaml.write_text(Path(__file__).parents[3].joinpath("config", "example.yaml").read_text())
    set_config_path(yaml)


def make_alert() -> Alert:
    return Alert(
        incident_fingerprint="fp-card-mention",
        service="payment-api",
        severity="critical",
        summary="CPU > 95%",
        labels={},
        lark_message_id="om_x",
        state=AlertState.firing,
        created_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    )


def test_render_user_mention_from_oncall_target() -> None:
    target = OncallTarget(
        source="fd_schedule",
        recipients=(
            OncallRecipient(
                kind="user",
                email="bob@company.com",
                user_id="ou_bob",
                display_name="Bob",
            ),
        ),
    )

    payload = render_firing(make_alert(), oncall_target=target)

    assert '<at user_id="ou_bob">Bob</at>' in _all_content(payload)


def test_render_role_mention_from_fallback_target() -> None:
    target = OncallTarget(
        source="fallback_role",
        recipients=(OncallRecipient(kind="role", role="@on-call"),),
    )

    payload = render_firing(make_alert(), oncall_target=target)

    assert "@on-call" in _all_content(payload)


def test_render_multiple_user_mentions_from_oncall_target() -> None:
    target = OncallTarget(
        source="static_map",
        recipients=(
            OncallRecipient(
                kind="user",
                email="alice@company.com",
                user_id="ou_alice",
                display_name="Alice",
            ),
            OncallRecipient(
                kind="user",
                email="bob@company.com",
                user_id="ou_bob",
                display_name="Bob",
            ),
        ),
    )

    payload = render_firing(make_alert(), oncall_target=target)
    content = _all_content(payload)

    assert '<at user_id="ou_alice">Alice</at> <at user_id="ou_bob">Bob</at>' in content


def test_render_without_target_preserves_us1_no_mention_behavior() -> None:
    payload = render_firing(make_alert())
    flat = json.dumps(payload, ensure_ascii=False)

    assert "<at " not in flat
    assert "@on-call" not in flat


def _all_content(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False).replace('\\"', '"')
