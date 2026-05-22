"""T010 — config 热重载与失败回退（FR-029 / SC-009 / Constitution V + VI）。

策略：
- 用 `set_config_path()` 切到 tmp 文件，立即 `get_config()` 见到 v1。
- 写入 v2，调用 `reload_config()` 同步重载（避免 watchdog 异步抖动）。
- 写入坏 YAML，`reload_config()` 必须保留旧 snapshot 并把异常往上抛，由 watcher 层捕获后调 meta-channel。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app import config as cfg_mod

GOLDEN_V1 = textwrap.dedent(
    """
    lark:
      app_id: "cli_v1"
      app_secret_env: "X"
      encrypt_key_env: "X"
      verification_token_env: "X"
      group_chat_id: "g"
      meta_channel_id: "m"
    flashduty:
      webhook_secret_env: "X"
      schedule_api_base: "https://x"
      schedule_api_token_env: "X"
    alertmanager:
      base_url: "http://x"
      service_account_token_env: "X"
      request_timeout_seconds: 5
    oncall:
      priority_chain: [incident_label, fd_schedule, static_map, fallback_role]
      static_service_map: {payment-api: ["alice@x"]}
      fallback_role: ["@on-call"]
      schedule_cache_ttl_seconds: 60
    severity_colors: {critical: red}
    silence_buttons: {fixed_durations: [5min, 30min], enable_custom: true}
    timezone: "Asia/Shanghai"
    max_silence_hours: 24
    """
)
GOLDEN_V2 = GOLDEN_V1.replace('"alice@x"', '"bob@x"')
BROKEN_YAML = "this is not: : valid yaml :"


@pytest.fixture(autouse=True)
def _reset_config_singleton() -> None:
    """每个测试前后复位 module-level snapshot，防串扰。"""
    cfg_mod._reset_for_tests()
    yield
    cfg_mod._reset_for_tests()


def test_reload_picks_up_new_value(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(GOLDEN_V1)
    cfg_mod.set_config_path(p)

    snap1 = cfg_mod.get_config()
    assert snap1.oncall.static_service_map["payment-api"] == ["alice@x"]

    p.write_text(GOLDEN_V2)
    cfg_mod.reload_config()
    snap2 = cfg_mod.get_config()
    assert snap2.oncall.static_service_map["payment-api"] == ["bob@x"]


def test_reload_with_broken_yaml_keeps_old_snapshot(tmp_path: Path) -> None:
    p = tmp_path / "live.yaml"
    p.write_text(GOLDEN_V1)
    cfg_mod.set_config_path(p)
    snap1 = cfg_mod.get_config()

    import yaml
    from pydantic import ValidationError

    p.write_text(BROKEN_YAML)
    with pytest.raises((yaml.YAMLError, ValidationError)):
        cfg_mod.reload_config()

    # 读到的仍是 v1，不应被坏 YAML 污染。
    snap_after = cfg_mod.get_config()
    assert snap_after is snap1


def test_atomic_swap_returns_same_object_until_reload(tmp_path: Path) -> None:
    """两次 get_config() 之间没有 reload — 必须是同一个对象（snapshot identity）。"""
    p = tmp_path / "live.yaml"
    p.write_text(GOLDEN_V1)
    cfg_mod.set_config_path(p)
    a = cfg_mod.get_config()
    b = cfg_mod.get_config()
    assert a is b
