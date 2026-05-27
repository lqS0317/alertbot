"""resolved card renderer keeps alert context."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import set_config_path
from app.models import Alert, AlertState
from app.services.cards import render_resolved


@pytest.fixture(autouse=True)
def _config(tmp_path: Path) -> None:
    yaml = tmp_path / "cfg.yaml"
    yaml.write_text(Path(__file__).parents[3].joinpath("config", "example.yaml").read_text())
    set_config_path(yaml)


def test_render_resolved_card_keeps_full_context_without_silence_dropdown() -> None:
    alert = Alert(
        incident_fingerprint="fp-resolved",
        service="kube-prometheus-stack",
        severity="warning",
        summary="Cluster has overcommitted memory resource requests.",
        labels={
            "alertname": "KubeMemoryOvercommit",
            "cluster": "hsk-ops-infra",
            "env": "ops",
            "instance": "10.39.156.252:4000",
        },
        annotations={
            "description": "Cluster hsk-ops-infra recovered from memory overcommit.",
            "runbook_url": "https://runbooks.example.com/kubememoryovercommit",
            "__generator_url": "https://vmalert.example.com/alert?id=1",
        },
        lark_message_id="om_x",
        state=AlertState.resolved,
        created_at=datetime(2026, 5, 27, 6, 20, 23, tzinfo=UTC),
    )

    payload = render_resolved(alert)
    flat = json.dumps(payload, ensure_ascii=False)

    assert payload["header"]["template"] == "green"
    assert "✅ [RESOLVED]" in flat
    assert "**✅ 恢复状态**：已恢复" in flat
    assert "**⏰ 恢复时间**：" in flat
    assert "**🧩 集群**：hsk-ops-infra" in flat
    assert "**🌐 环境**：ops" in flat
    assert "**🔧 服务**：kube-prometheus-stack" in flat
    assert "**🔥 严重程度**：Warning" in flat
    assert "**📍 告警对象**：10.39.156.252:4000" in flat
    assert "Cluster hsk-ops-infra recovered from memory overcommit." in flat
    assert "[查看 Runbook](https://runbooks.example.com/kubememoryovercommit)" in flat
    assert "[在监控系统查看](https://vmalert.example.com/alert?id=1)" in flat
    assert "选择静默时长" not in flat
    assert '"tag": "select_static"' not in flat
