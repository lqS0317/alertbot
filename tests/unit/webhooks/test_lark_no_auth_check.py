"""T069 — FR-023 regression: silence buttons have no authorization gate."""

from __future__ import annotations

from pathlib import Path


def test_lark_webhook_contains_no_oncall_admin_or_role_authorization_gate() -> None:
    source = Path("app/webhooks/lark.py").read_text()
    forbidden = ("is_oncall", "is_admin", "role_check")
    for token in forbidden:
        assert token not in source
