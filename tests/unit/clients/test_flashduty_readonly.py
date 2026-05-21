"""T052 — FlashDuty client public surface stays read-only for incident state (FR-024)."""

from __future__ import annotations

import inspect

import app.clients.flashduty as flashduty


def test_flashduty_client_exposes_no_incident_mutators() -> None:
    forbidden_fragments = ("ack", "close", "snooze", "mutate", "update_incident")
    public_callables = {
        name
        for name, obj in vars(flashduty).items()
        if not name.startswith("_")
        and callable(obj)
        and getattr(obj, "__module__", "") == flashduty.__name__
    }

    assert "FlashDutyClient" in public_callables
    assert "read_schedule" not in public_callables  # method, not module-level mutator
    for name in public_callables:
        lowered = name.lower()
        assert not any(fragment in lowered for fragment in forbidden_fragments), name


def test_flashduty_client_has_read_schedule_method_only_for_external_api() -> None:
    methods = {
        name
        for name, member in inspect.getmembers(
            flashduty.FlashDutyClient, predicate=inspect.isfunction
        )
        if not name.startswith("_")
    }

    assert "read_schedule" in methods
    assert methods == {"read_schedule", "aclose"}
