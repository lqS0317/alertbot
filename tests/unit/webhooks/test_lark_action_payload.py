"""T061 — Lark card.action.trigger payload parser."""

from __future__ import annotations

from app.clients.lark import parse_lark_action_event


def test_parse_lark_card_action_trigger_payload() -> None:
    event = parse_lark_action_event(
        {
            "header": {"event_id": "evt-1", "event_type": "card.action.trigger"},
            "event": {
                "operator": {"user_id": "ou_alice"},
                "action": {
                    "value": {
                        "kind": "silence",
                        "alert_fingerprint": "fp-123",
                        "duration": "30min",
                    }
                },
            },
        }
    )

    assert event.event_id == "evt-1"
    assert event.operator_user_id == "ou_alice"
    assert event.kind == "silence"
    assert event.alert_fingerprint == "fp-123"
    assert event.duration == "30min"
