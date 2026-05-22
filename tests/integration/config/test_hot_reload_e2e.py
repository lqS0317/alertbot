"""T093 — SC-009 config hot-reload E2E guard."""

from __future__ import annotations

from pathlib import Path

from app import config as cfg_mod


def test_hot_reload_changes_static_service_map_without_restart(tmp_path: Path) -> None:
    cfg_mod._reset_for_tests()
    path = tmp_path / "config.yaml"
    path.write_text(_yaml_for("alice@company.com"))
    cfg_mod.set_config_path(path)
    assert cfg_mod.get_config().oncall.static_service_map["payment-api"] == ["alice@company.com"]

    path.write_text(_yaml_for("bob@company.com"))
    cfg_mod.reload_config()

    assert cfg_mod.get_config().oncall.static_service_map["payment-api"] == ["bob@company.com"]


def _yaml_for(email: str) -> str:
    return f"""
lark:
  app_id: "cli"
  app_secret_env: "X"
  encrypt_key_env: "X"
  verification_token_env: "X"
  group_chat_id: "g"
  meta_channel_id: "m"
flashduty:
  webhook_secret_env: "X"
  schedule_api_base: "https://fd.test"
  schedule_api_token_env: "X"
alertmanager:
  base_url: "http://am.test"
  service_account_token_env: "X"
  request_timeout_seconds: 5
oncall:
  priority_chain: [incident_label, fd_schedule, static_map, fallback_role]
  incident_label_key: "lark_user"
  static_service_map:
    payment-api: ["{email}"]
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
