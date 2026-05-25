"""T009 — app/config.py Pydantic schema 校验。

覆盖 spec FR-029 / Constitution V（Config-Driven）。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import AlertBotConfig, load_config_from_yaml

GOLDEN_YAML = textwrap.dedent(
    """
    lark:
      app_id: "cli_abc"
      app_secret_env: "LARK_APP_SECRET"
      encrypt_key_env: "LARK_ENCRYPT_KEY"
      verification_token_env: "LARK_VERIFY_TOKEN"
      group_chat_id: "oc_group"
      meta_channel_id: "oc_meta"
    flashduty:
      webhook_secret_env: "FD_WEBHOOK_SECRET"
      schedule_api_base: "https://api.flashcat.cloud/api/v1"
      schedule_api_token_env: "FD_API_TOKEN"
    alertmanager:
      base_url: "http://localhost:9093"
      service_account_token_env: "AM_TOKEN"
      request_timeout_seconds: 5
    oncall:
      priority_chain: [incident_label, fd_schedule, static_map, fallback_role]
      incident_label_key: "owner_email"
      static_service_map:
        payment-api: ["alice@company.com", "bob@company.com"]
      fallback_role: ["@on-call"]
      schedule_cache_ttl_seconds: 300
    severity_colors:
      critical: red
      warning: orange
      info: blue
    silence_buttons:
      fixed_durations: [5min, 30min, 1h, 4h, 24h]
      enable_custom: true
    timezone: "Asia/Shanghai"
    max_silence_hours: 24
    """
)


def test_golden_yaml_loads_and_validates(tmp_path: Path) -> None:
    p = tmp_path / "ok.yaml"
    p.write_text(GOLDEN_YAML)
    cfg = load_config_from_yaml(p)
    assert isinstance(cfg, AlertBotConfig)
    assert cfg.timezone == "Asia/Shanghai"
    assert cfg.max_silence_hours == 24
    assert cfg.oncall.incident_label_key == "owner_email"
    assert cfg.oncall.static_service_map["payment-api"] == [
        "alice@company.com",
        "bob@company.com",
    ]
    assert cfg.oncall.fallback_role == ["@on-call"]
    assert cfg.oncall.schedule_cache_ttl_seconds == 300
    assert cfg.silence_buttons.fixed_durations == ["5min", "30min", "1h", "4h", "24h"]


def test_max_silence_hours_above_24_rejected(tmp_path: Path) -> None:
    """FR-017: 24h 是硬上限，配置不允许提到 25h."""
    p = tmp_path / "too_high.yaml"
    p.write_text(GOLDEN_YAML.replace("max_silence_hours: 24", "max_silence_hours: 25"))
    with pytest.raises(ValidationError):
        load_config_from_yaml(p)


def test_schedule_cache_ttl_above_300_rejected(tmp_path: Path) -> None:
    """FR-013: 缓存 TTL ≤ 5 分钟（300s）。"""
    p = tmp_path / "bad_ttl.yaml"
    p.write_text(
        GOLDEN_YAML.replace("schedule_cache_ttl_seconds: 300", "schedule_cache_ttl_seconds: 600")
    )
    with pytest.raises(ValidationError):
        load_config_from_yaml(p)


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    """extra='forbid' 防止配置文件里出现未知字段。"""
    p = tmp_path / "extra.yaml"
    p.write_text(GOLDEN_YAML + "\nmystery_key: 42\n")
    with pytest.raises(ValidationError):
        load_config_from_yaml(p)


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    import yaml

    p = tmp_path / "broken.yaml"
    p.write_text("not: valid: : yaml :")
    with pytest.raises((yaml.YAMLError, ValidationError)):
        load_config_from_yaml(p)


def test_config_is_frozen(tmp_path: Path) -> None:
    """配置 snapshot 必须不可变（frozen=True），保证读者拿到的永远是一致快照。"""
    p = tmp_path / "ok.yaml"
    p.write_text(GOLDEN_YAML)
    cfg = load_config_from_yaml(p)
    with pytest.raises(ValidationError):
        cfg.timezone = "UTC"  # type: ignore[misc]


def test_empty_static_map_recipient_list_rejected(tmp_path: Path) -> None:
    p = tmp_path / "empty_static_map.yaml"
    p.write_text(
        GOLDEN_YAML.replace(
            'payment-api: ["alice@company.com", "bob@company.com"]',
            "payment-api: []",
        )
    )
    with pytest.raises(ValidationError):
        load_config_from_yaml(p)


def test_empty_fallback_role_rejected(tmp_path: Path) -> None:
    p = tmp_path / "empty_fallback.yaml"
    p.write_text(GOLDEN_YAML.replace('fallback_role: ["@on-call"]', "fallback_role: []"))
    with pytest.raises(ValidationError):
        load_config_from_yaml(p)
