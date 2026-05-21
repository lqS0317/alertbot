"""T083 — custom silence duration parser."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.services.cards import parse_duration


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1min", timedelta(minutes=1)),
        ("5min", timedelta(minutes=5)),
        ("30min", timedelta(minutes=30)),
        ("1h", timedelta(hours=1)),
        ("7h", timedelta(hours=7)),
        ("24h", timedelta(hours=24)),
    ],
)
def test_parse_duration_accepts_valid_values(value: str, expected: timedelta) -> None:
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["", "0min", "25h", "banana", "NaN", "1d", "90m"])
def test_parse_duration_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)
