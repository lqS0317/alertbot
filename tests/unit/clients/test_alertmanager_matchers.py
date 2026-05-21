"""T062 — Alertmanager matcher translation."""

from __future__ import annotations

from app.clients.alertmanager import matchers_from_labels


def test_matchers_from_labels_excludes_alertbot_internal_keys() -> None:
    labels = {
        "alertname": "HighCPU",
        "instance": "web-01",
        "lark_user": "alice@company.com",
        "flashduty_team": "sre",
    }

    assert matchers_from_labels(labels) == [
        {"name": "alertname", "value": "HighCPU", "isRegex": False, "isEqual": True},
        {"name": "instance", "value": "web-01", "isRegex": False, "isEqual": True},
    ]
