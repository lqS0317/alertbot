"""新增字段（详细描述 / 环境 / 处理手册 / 查看监控 / 链接重写）的渲染测试。

覆盖：
  * alertmanager_inbound.alert_to_event 把 annotations + generator_url 透传到 Incident
  * render_firing 新字段映射（description / env / runbook / generator）
  * cfg.cards.links.runbook_default_url 兜底
  * cfg.cards.links.generator_url_rewrites 前缀重写
  * _safe_link 拒绝非 http(s) scheme（安全默认）
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from app.clients.alertmanager_inbound import AlertmanagerInboundAlert, alert_to_event
from app.config import reload_config, set_config_path
from app.models import Alert, AlertState
from app.services import cards as cards_module
from app.services.cards import render_firing


def _write_cfg(tmp_path: Path, *, links: dict | None = None) -> None:
    """基于 example.yaml 复制一份，覆盖 cards.links 段。"""
    base = yaml.safe_load(Path(__file__).parents[3].joinpath("config", "example.yaml").read_text())
    if links is not None:
        base.setdefault("cards", {}).setdefault("links", {})
        base["cards"]["links"].update(links)
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True))
    set_config_path(path)
    reload_config()


@pytest.fixture(autouse=True)
def _config(tmp_path: Path) -> None:
    _write_cfg(tmp_path)


def _make_alert(**overrides) -> Alert:
    defaults: dict = dict(
        incident_fingerprint="6aaada8482dcb502",
        service="kube-prometheus-stack",
        severity="warning",
        summary="Cluster has overcommitted memory resource requests.",
        labels={
            "alertgroup": "kubernetes-resources",
            "alertname": "KubeMemoryOvercommit",
            "cluster": "hsk-ops-infra",
            "env": "ops",
            "severity": "warning",
        },
        annotations={
            "description": (
                "Cluster hsk-ops-infra has overcommitted memory resource requests "
                "for Pods by 4.15G bytes and cannot tolerate node failure."
            ),
            "runbook_url": (
                "https://runbooks.prometheus-operator.dev/runbooks/kubernetes/kubememoryovercommit"
            ),
            "summary": "Cluster has overcommitted memory resource requests.",
            "__generator_url": (
                "http://vmalert-ops-859f5b67-cgzk7:8080/vmalert/alert?group_id=1&alert_id=2"
            ),
        },
        lark_message_id="om_x",
        state=AlertState.firing,
        created_at=datetime(2026, 5, 13, 8, 2, 47, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Alert(**defaults)


# ───────────────────────── alert_to_event 透传 ──────────────────────────────


def test_alert_to_event_passes_annotations_and_generator_url_into_incident_annotations() -> None:
    inbound = AlertmanagerInboundAlert.model_validate(
        {
            "status": "firing",
            "labels": {"alertname": "X", "severity": "warning"},
            "annotations": {"description": "boom", "runbook_url": "https://r.example.com/x"},
            "startsAt": "2026-05-13T08:02:47Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "fp-1",
            "generatorURL": "http://vmalert.internal:8080/x",
        }
    )

    event = alert_to_event(inbound)

    assert event.incident.annotations["description"] == "boom"
    assert event.incident.annotations["runbook_url"] == "https://r.example.com/x"
    assert event.incident.annotations["__generator_url"] == "http://vmalert.internal:8080/x"


def test_alert_to_event_omits_generator_url_key_when_field_missing() -> None:
    inbound = AlertmanagerInboundAlert.model_validate(
        {
            "status": "firing",
            "labels": {"alertname": "X", "severity": "warning"},
            "annotations": {"summary": "s"},
            "startsAt": "2026-05-13T08:02:47Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "fp-2",
        }
    )

    event = alert_to_event(inbound)

    assert "__generator_url" not in event.incident.annotations
    assert event.incident.annotations["summary"] == "s"


# ───────────────────────── render_firing 新字段 ──────────────────────────────


def test_render_firing_uses_description_over_summary() -> None:
    payload = render_firing(_make_alert())
    flat = json.dumps(payload, ensure_ascii=False)

    assert "cannot tolerate node failure" in flat  # description 文本
    assert "**🔍 故障描述**：" in flat


def test_render_firing_renders_env_field() -> None:
    payload = render_firing(_make_alert())
    flat = json.dumps(payload, ensure_ascii=False)

    assert "**🌐 环境**：ops" in flat


def test_render_firing_renders_runbook_link_from_annotations() -> None:
    payload = render_firing(_make_alert())
    flat = json.dumps(payload, ensure_ascii=False)

    assert "**📖 处理手册**：" in flat
    assert "[查看 Runbook](" in flat
    assert "runbooks.prometheus-operator.dev" in flat


def test_render_firing_runbook_falls_back_to_config_default(tmp_path: Path) -> None:
    _write_cfg(tmp_path, links={"runbook_default_url": "https://wiki.example.com/oncall"})
    alert = _make_alert(
        annotations={"description": "d", "summary": "s"},  # no runbook_url
    )

    payload = render_firing(alert)
    flat = json.dumps(payload, ensure_ascii=False)

    assert "[查看 Runbook](https://wiki.example.com/oncall)" in flat


def test_render_firing_runbook_dash_when_no_payload_and_no_config(tmp_path: Path) -> None:
    _write_cfg(tmp_path)  # default empty runbook_default_url
    alert = _make_alert(annotations={"description": "d", "summary": "s"})

    payload = render_firing(alert)
    flat = json.dumps(payload, ensure_ascii=False)

    assert "**📖 处理手册**：-" in flat


def test_render_firing_rewrites_generator_url_using_config(tmp_path: Path) -> None:
    _write_cfg(
        tmp_path,
        links={
            "generator_url_rewrites": [
                {
                    "from": "http://vmalert-ops-859f5b67-cgzk7:8080",
                    "to": "https://vmalert.ops.example.com",
                },
                {
                    "from": "http://vmalert-prod",
                    "to": "https://vmalert.prod.example.com",
                },
            ],
        },
    )

    payload = render_firing(_make_alert())
    flat = json.dumps(payload, ensure_ascii=False)

    assert "https://vmalert.ops.example.com/vmalert/alert?group_id=1&alert_id=2" in flat
    assert "vmalert-ops-859f5b67-cgzk7:8080" not in flat  # 原内部地址不应再出现


def test_render_firing_generator_dash_when_payload_missing() -> None:
    alert = _make_alert(annotations={"description": "d"})  # no __generator_url

    payload = render_firing(alert)
    flat = json.dumps(payload, ensure_ascii=False)

    assert "**🔗 查看监控**：-" in flat


# ───────────────────────── _safe_link 安全兜底 ──────────────────────────────


def test_safe_link_allows_https_and_http() -> None:
    assert cards_module._safe_link("x", "https://a.example.com") == "[x](https://a.example.com)"
    assert cards_module._safe_link("x", "http://a.example.com") == "[x](http://a.example.com)"


def test_safe_link_rejects_javascript_and_other_schemes() -> None:
    # javascript: / file: / data: 等绝不能渲染成可点击链接
    assert cards_module._safe_link("x", "javascript:alert(1)") == "javascript:alert(1)"
    assert cards_module._safe_link("x", "file:///etc/passwd") == "file:///etc/passwd"
    assert cards_module._safe_link("x", "") == "-"


# ───────────────────────── _rewrite_url 行为 ────────────────────────────────


def test_rewrite_url_first_match_wins() -> None:
    from app.config import CardLinkRewrite

    rules = [
        CardLinkRewrite(**{"from": "http://a:8080", "to": "https://a.example.com"}),
        CardLinkRewrite(**{"from": "http://a", "to": "https://wrong.example.com"}),
    ]
    assert (
        cards_module._rewrite_url("http://a:8080/path?x=1", rules)
        == "https://a.example.com/path?x=1"
    )


def test_rewrite_url_no_match_returns_original() -> None:
    from app.config import CardLinkRewrite

    rules = [CardLinkRewrite(**{"from": "http://b", "to": "https://b.example.com"})]
    assert cards_module._rewrite_url("http://a/path", rules) == "http://a/path"
