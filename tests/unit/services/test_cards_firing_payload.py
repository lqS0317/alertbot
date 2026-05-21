"""T029 — services.cards.render_firing 卡片 payload 单元测试 (US1, no buttons / no @-mention)。

US1 阶段卡片只有：severity-coloured 标题、服务名、时间（团队时区）、summary。
按钮 + @ 在 US2/US3 才加。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import set_config_path
from app.models import Alert, AlertState
from app.services.cards import render_firing


@pytest.fixture(autouse=True)
def _config(tmp_path: Path) -> None:
    """卡片渲染要读 config（severity_colors / timezone），临时写一份。"""
    yaml = tmp_path / "cfg.yaml"
    yaml.write_text(Path(__file__).parents[3].joinpath("config", "example.yaml").read_text())
    set_config_path(yaml)


def _make_alert(severity: str = "critical") -> Alert:
    return Alert(
        incident_fingerprint="alertname=HighCPU,instance=web-01",
        service="payment-api",
        severity=severity,
        summary="CPU > 95% for 5m on web-01",
        labels={"alertname": "HighCPU", "instance": "web-01"},
        lark_message_id="om_msg_placeholder",
        state=AlertState.firing,
        created_at=datetime(2026, 5, 7, 8, 0, 0, tzinfo=UTC),
    )


def test_render_firing_uses_severity_colour_from_config() -> None:
    payload = render_firing(_make_alert(severity="critical"))
    # 卡片 header 颜色必须是 critical → red
    assert _find_template_color(payload) == "red"

    payload_w = render_firing(_make_alert(severity="warning"))
    assert _find_template_color(payload_w) == "orange"


def test_render_firing_includes_service_summary_and_time_in_team_tz() -> None:
    payload = render_firing(_make_alert())
    flat = json.dumps(payload, ensure_ascii=False)
    assert "payment-api" in flat
    assert "CPU > 95% for 5m on web-01" in flat
    # Asia/Shanghai = UTC+8 → 16:00
    assert "16:00" in flat


def test_render_firing_has_no_action_buttons_in_us1() -> None:
    """US1 阶段不渲染按钮（按钮是 US3 加的，US1 only 做可见性 + 复原）。"""
    payload = render_firing(_make_alert())
    flat = json.dumps(payload, ensure_ascii=False).lower()
    # 没有 button 元素
    assert '"tag": "button"' not in flat
    # 没有 action / interactive button container
    assert '"actions"' not in flat


def test_render_firing_has_no_at_mention_in_us1() -> None:
    """US1 阶段不带 @人，@ 是 US2 才接入。"""
    payload = render_firing(_make_alert())
    flat = json.dumps(payload, ensure_ascii=False)
    assert "<at " not in flat
    assert "@on-call" not in flat


# ───────────────────────── helpers ──────────────────────────────────


def _find_template_color(payload: dict[str, object]) -> str:
    header = payload.get("header")
    assert isinstance(header, dict), "card payload must have a header object"
    template = header.get("template")
    assert isinstance(template, str), "card header.template must be a colour string"
    return template
