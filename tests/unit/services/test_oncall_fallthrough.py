"""T049 — on-call fallthrough and meta-channel reporting."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.config import set_config_path
from app.models import Alert, AlertState
from app.services.oncall import OncallResolver


@dataclass
class FailingFlashDutyClient:
    calls: int = 0

    async def read_schedule(self, service: str) -> str | None:
        self.calls += 1
        raise RuntimeError("fd schedule unavailable")


@dataclass
class FakeLarkClient:
    by_email: dict[str, tuple[str, str]]

    async def lookup_user_by_email(self, email: str) -> tuple[str, str] | None:
        return self.by_email.get(email)


@pytest.fixture
def config_with_static(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
lark: {app_id: "cli", app_secret_env: "X", encrypt_key_env: "X", verification_token_env: "X", group_chat_id: "g", meta_channel_id: "m"}
flashduty: {webhook_secret_env: "X", schedule_api_base: "https://fd.test", schedule_api_token_env: "X"}
alertmanager: {base_url: "http://am", service_account_token_env: "X", request_timeout_seconds: 5}
oncall:
  priority_chain: [incident_label, fd_schedule, static_map, fallback_role]
  static_service_map: {payment-api: ["carol@company.com"]}
  fallback_role: ["@on-call"]
  schedule_cache_ttl_seconds: 300
severity_colors: {critical: red}
silence_buttons: {fixed_durations: [5min, 30min, 1h, 4h, 24h], enable_custom: true}
timezone: "Asia/Shanghai"
max_silence_hours: 24
""".strip()
    )
    set_config_path(path)


@pytest.fixture
def config_without_static(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
lark: {app_id: "cli", app_secret_env: "X", encrypt_key_env: "X", verification_token_env: "X", group_chat_id: "g", meta_channel_id: "m"}
flashduty: {webhook_secret_env: "X", schedule_api_base: "https://fd.test", schedule_api_token_env: "X"}
alertmanager: {base_url: "http://am", service_account_token_env: "X", request_timeout_seconds: 5}
oncall:
  priority_chain: [incident_label, fd_schedule, static_map, fallback_role]
  static_service_map: {}
  fallback_role: ["@on-call"]
  schedule_cache_ttl_seconds: 300
severity_colors: {critical: red}
silence_buttons: {fixed_durations: [5min, 30min, 1h, 4h, 24h], enable_custom: true}
timezone: "Asia/Shanghai"
max_silence_hours: 24
""".strip()
    )
    set_config_path(path)


def make_alert(labels: dict[str, Any] | None = None, service: str = "payment-api") -> Alert:
    return Alert(
        incident_fingerprint="fp-fallthrough",
        service=service,
        severity="critical",
        summary="CPU high",
        labels=labels or {},
        lark_message_id="om_x",
        state=AlertState.firing,
    )


@pytest.mark.asyncio
async def test_fd_failure_falls_to_static_map_and_reports_meta(config_with_static: None) -> None:
    reporter = AsyncMock()
    reporter.report = AsyncMock(return_value=None)
    resolver = OncallResolver(
        flashduty=FailingFlashDutyClient(),
        lark=FakeLarkClient({"carol@company.com": ("ou_carol", "Carol")}),
        reporter=reporter,
    )

    target = await resolver.resolve(make_alert())

    assert target.source == "static_map"
    assert target.email == "carol@company.com"
    reporter.report.assert_awaited_once()


@pytest.mark.asyncio
async def test_fd_failure_and_empty_static_falls_to_role(config_without_static: None) -> None:
    reporter = AsyncMock()
    reporter.report = AsyncMock(return_value=None)
    resolver = OncallResolver(
        flashduty=FailingFlashDutyClient(),
        lark=FakeLarkClient({}),
        reporter=reporter,
    )

    target = await resolver.resolve(make_alert())

    assert target.source == "fallback_role"
    assert target.role == "@on-call"
    reporter.report.assert_awaited_once()


@pytest.fixture
def config_fallback_with_email(tmp_path: Path) -> None:
    """fallback_role 里混用 email + role 文本 — 验证新的自适应行为。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        """
lark: {app_id: "cli", app_secret_env: "X", encrypt_key_env: "X", verification_token_env: "X", group_chat_id: "g", meta_channel_id: "m"}
flashduty: {webhook_secret_env: "X", schedule_api_base: "https://fd.test", schedule_api_token_env: "X"}
alertmanager: {base_url: "http://am", service_account_token_env: "X", request_timeout_seconds: 5}
oncall:
  priority_chain: [incident_label, fd_schedule, static_map, fallback_role]
  static_service_map: {}
  fallback_role: ["sunyu@hashkey.cloud", "@on-call"]
  schedule_cache_ttl_seconds: 300
severity_colors: {critical: red}
silence_buttons: {fixed_durations: [5min, 30min, 1h, 4h, 24h], enable_custom: true}
timezone: "Asia/Shanghai"
max_silence_hours: 24
""".strip()
    )
    set_config_path(path)


@pytest.mark.asyncio
async def test_fallback_role_email_item_resolves_to_user_via_lookup(
    config_fallback_with_email: None,
) -> None:
    """fallback_role 元素含 email 形态 → 走 lookup → 渲染成 user 类型 recipient。"""
    reporter = AsyncMock()
    reporter.report = AsyncMock(return_value=None)
    resolver = OncallResolver(
        flashduty=FailingFlashDutyClient(),
        lark=FakeLarkClient({"sunyu@hashkey.cloud": ("ou_sunyu", "Sun Yu")}),
        reporter=reporter,
    )

    target = await resolver.resolve(make_alert())

    assert target.source == "fallback_role"
    assert len(target.recipients) == 2
    # email 项 → user 类型 + 真实 user_id
    assert target.recipients[0].kind == "user"
    assert target.recipients[0].user_id == "ou_sunyu"
    # role 文本项 → role 类型
    assert target.recipients[1].kind == "role"
    assert target.recipients[1].role == "@on-call"
    # mention_text 拼出蓝色 @ + role 文本（兜底两层信息）
    rendered = target.mention_text()
    assert "<at id=ou_sunyu></at>" in rendered
    assert "@on-call" in rendered


@pytest.mark.asyncio
async def test_fallback_role_email_lookup_failure_degrades_to_plaintext(
    config_fallback_with_email: None,
) -> None:
    """fallback_role 含 email 但 lookup 失败（找不到 user_id）→ 必须降级为**纯文本**
    邮箱，**不能**用 `<at email=…></at>`。

    设计理由：飞书对 `<at email=>` 会做 email 存在性校验，配置里拼错一个邮箱字符
    就会让整张卡 400 拒卡（ErrCode 100290 "invalid user resource"），把告警直接
    打丢。降级为纯文本：飞书永远接受，运维肉眼也能看出谁配错了。
    """
    reporter = AsyncMock()
    reporter.report = AsyncMock(return_value=None)
    resolver = OncallResolver(
        flashduty=FailingFlashDutyClient(),
        lark=FakeLarkClient({}),  # 空 cache → lookup 返回 None
        reporter=reporter,
    )

    target = await resolver.resolve(make_alert())

    assert target.recipients[0].kind == "user"
    assert target.recipients[0].user_id is None
    assert target.recipients[0].email == "sunyu@hashkey.cloud"
    rendered = target.mention_text()
    # MUST: 不再使用 <at email=…></at>（飞书会拒卡）
    assert "<at email=" not in rendered
    # 显示纯文本邮箱
    assert "sunyu@hashkey.cloud" in rendered
