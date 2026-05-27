"""T061 — Lark card.action.trigger payload parser."""

from __future__ import annotations

import json

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


def test_parse_lark_select_static_option_payload() -> None:
    """JSON 2.0 select_static（非 form 容器）把选中项放在 action.option。"""
    event = parse_lark_action_event(
        {
            "schema": "2.0",
            "header": {"event_id": "evt-select-1", "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_alice"},
                "action": {
                    "tag": "select_static",
                    "option": json.dumps(
                        {
                            "kind": "silence",
                            "alert_fingerprint": "fp-select",
                            "duration": "5min",
                        },
                        separators=(",", ":"),
                    ),
                },
            },
        }
    )

    assert event.event_id == "evt-select-1"
    assert event.operator_user_id == "ou_alice"
    assert event.kind == "silence"
    assert event.alert_fingerprint == "fp-select"
    assert event.duration == "5min"
