"""T063 — silenced card renderer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import set_config_path
from app.models import Alert, AlertState, Silence, SilenceState
from app.services.cards import render_silenced


@pytest.fixture(autouse=True)
def _config(tmp_path: Path) -> None:
    yaml = tmp_path / "cfg.yaml"
    yaml.write_text(Path(__file__).parents[3].joinpath("config", "example.yaml").read_text())
    set_config_path(yaml)


def test_render_silenced_card_keeps_full_context_without_silence_dropdown() -> None:
    now = datetime(2026, 5, 7, 8, 0, tzinfo=UTC)
    alert = Alert(
        incident_fingerprint="fp-silenced",
        service="payment-api",
        severity="critical",
        summary="CPU high",
        labels={
            "alertname": "HighCPU",
            "cluster": "hsk-ops-infra",
            "env": "ops",
            "instance": "10.39.156.252:4000",
        },
        annotations={
            "description": "CPU > 95% for 5m on 10.39.156.252",
            "runbook_url": "https://runbooks.example.com/highcpu",
            "__generator_url": "https://vmalert.example.com/alert?id=1",
        },
        lark_message_id="om_x",
        state=AlertState.silenced,
        created_at=now,
    )
    silence = Silence(
        alertmanager_silence_id="am-1",
        lark_event_id="evt-1",
        alert_fingerprint="fp-silenced",
        matchers=[],
        created_by="alice@company.com",
        actor_lark_user_id="ou_alice",
        starts_at=now,
        ends_at=now + timedelta(minutes=30),
        duration_choice="30min",
        state=SilenceState.active,
    )

    payload = render_silenced(alert, silence, operator_name="Alice")
    flat = json.dumps(payload, ensure_ascii=False)

    assert payload["header"]["template"] == "grey"
    assert "🔕 [SILENCED]" in flat
    assert "**🔕 静默状态**：已静默" in flat
    assert "**👤 操作人**：Alice" in flat
    assert "**⏳ 静默到期**：" in flat
    assert "16:30" in flat
    # 保留 firing 卡里的完整排障上下文，静默后回看也能定位对象。
    assert "**🧩 集群**：hsk-ops-infra" in flat
    assert "**🌐 环境**：ops" in flat
    assert "**🔧 服务**：payment-api" in flat
    assert "**🔥 严重程度**：Critical" in flat
    assert "**📍 告警对象**：10.39.156.252:4000" in flat
    assert "CPU > 95% for 5m on 10.39.156.252" in flat
    assert "[查看 Runbook](https://runbooks.example.com/highcpu)" in flat
    assert "[在监控系统查看](https://vmalert.example.com/alert?id=1)" in flat
    # 已静默卡不再展示下拉框，避免重复操作同一张卡。
    assert "选择静默时长" not in flat
    assert '"tag": "select_static"' not in flat
    assert '"tag": "button"' not in flat
