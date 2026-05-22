"""T048 — D-plan on-call priority chain.

优先级必须是：
incident.labels.lark_user → FlashDuty schedule → static service map → fallback role.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.config import set_config_path
from app.models import Alert, AlertState
from app.services.oncall import OncallResolver


@dataclass
class FakeFlashDutyClient:
    email: str | None = None
    calls: int = 0

    async def read_schedule(self, service: str) -> str | None:
        self.calls += 1
        return self.email


@dataclass
class FakeLarkClient:
    by_email: dict[str, tuple[str, str]]
    calls_by_email: list[str]

    async def lookup_user_by_email(self, email: str) -> tuple[str, str] | None:
        self.calls_by_email.append(email)
        return self.by_email.get(email)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
lark:
  app_id: "cli_test"
  app_secret_env: "X"
  encrypt_key_env: "X"
  verification_token_env: "X"
  group_chat_id: "oc_group"
  meta_channel_id: "oc_meta"
flashduty:
  webhook_secret_env: "X"
  schedule_api_base: "https://fd.test/api/v1"
  schedule_api_token_env: "X"
alertmanager:
  base_url: "http://am.test"
  service_account_token_env: "X"
  request_timeout_seconds: 5
oncall:
  priority_chain: [incident_label, fd_schedule, static_map, fallback_role]
  incident_label_key: "owner_email"
  static_service_map:
    payment-api: ["carol@company.com"]
  fallback_role: ["@on-call"]
  schedule_cache_ttl_seconds: 300
severity_colors:
  critical: red
silence_buttons:
  fixed_durations: [5min, 30min, 1h, 4h, 24h]
  enable_custom: true
timezone: "Asia/Shanghai"
max_silence_hours: 24
""".strip()
    )
    set_config_path(path)
    return path


def make_alert(labels: dict[str, Any] | None = None, service: str = "payment-api") -> Alert:
    return Alert(
        incident_fingerprint="fp-priority",
        service=service,
        severity="critical",
        summary="CPU high",
        labels=labels or {},
        lark_message_id="om_x",
        state=AlertState.firing,
    )


@pytest.mark.asyncio
async def test_incident_label_wins_and_skips_lower_tiers(config_path: Path) -> None:
    fd = FakeFlashDutyClient(email="bob@company.com")
    lark = FakeLarkClient(
        by_email={"alice@company.com": ("ou_alice", "Alice")},
        calls_by_email=[],
    )
    resolver = OncallResolver(flashduty=fd, lark=lark)

    target = await resolver.resolve(make_alert({"owner_email": "alice@company.com"}))

    assert target.source == "incident_label"
    assert target.email == "alice@company.com"
    assert target.user_id == "ou_alice"
    assert fd.calls == 0
    assert lark.calls_by_email == ["alice@company.com"]


@pytest.mark.asyncio
async def test_incident_label_uses_configured_label_key(config_path: Path) -> None:
    fd = FakeFlashDutyClient(email=None)
    lark = FakeLarkClient(
        by_email={"alice@company.com": ("ou_alice", "Alice")},
        calls_by_email=[],
    )
    resolver = OncallResolver(flashduty=fd, lark=lark)

    target = await resolver.resolve(make_alert({"lark_user": "ignored", "owner_email": "alice@company.com"}))

    assert target.source == "incident_label"
    assert target.email == "alice@company.com"


@pytest.mark.asyncio
async def test_flashduty_schedule_wins_over_static_map(config_path: Path) -> None:
    fd = FakeFlashDutyClient(email="bob@company.com")
    lark = FakeLarkClient(
        by_email={"bob@company.com": ("ou_bob", "Bob")},
        calls_by_email=[],
    )
    resolver = OncallResolver(flashduty=fd, lark=lark)

    target = await resolver.resolve(make_alert())

    assert target.source == "fd_schedule"
    assert target.email == "bob@company.com"
    assert target.user_id == "ou_bob"
    assert fd.calls == 1


@pytest.mark.asyncio
async def test_static_map_wins_over_fallback_role(config_path: Path) -> None:
    fd = FakeFlashDutyClient(email=None)
    lark = FakeLarkClient(
        by_email={"carol@company.com": ("ou_carol", "Carol")},
        calls_by_email=[],
    )
    resolver = OncallResolver(flashduty=fd, lark=lark)

    target = await resolver.resolve(make_alert())

    assert target.source == "static_map"
    assert target.email == "carol@company.com"
    assert target.user_id == "ou_carol"


@pytest.mark.asyncio
async def test_static_map_can_return_multiple_users(config_path: Path) -> None:
    path = config_path
    path.write_text(path.read_text().replace(
        'payment-api: ["carol@company.com"]',
        'payment-api: ["carol@company.com", "dave@company.com"]',
    ))
    set_config_path(path)
    fd = FakeFlashDutyClient(email=None)
    lark = FakeLarkClient(
        by_email={
            "carol@company.com": ("ou_carol", "Carol"),
            "dave@company.com": ("ou_dave", "Dave"),
        },
        calls_by_email=[],
    )
    resolver = OncallResolver(flashduty=fd, lark=lark)

    target = await resolver.resolve(make_alert())

    assert target.source == "static_map"
    assert [recipient.email for recipient in target.recipients] == [
        "carol@company.com",
        "dave@company.com",
    ]
    assert [recipient.user_id for recipient in target.recipients] == ["ou_carol", "ou_dave"]


@pytest.mark.asyncio
async def test_fallback_role_when_no_user_tier_matches(config_path: Path) -> None:
    fd = FakeFlashDutyClient(email=None)
    lark = FakeLarkClient(by_email={}, calls_by_email=[])
    resolver = OncallResolver(flashduty=fd, lark=lark)

    target = await resolver.resolve(make_alert(service="unknown-svc"))

    assert target.source == "fallback_role"
    assert target.role == "@on-call"
    assert target.user_id is None
