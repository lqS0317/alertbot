"""T051 — firing card @-mention rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import set_config_path
from app.models import Alert, AlertState
from app.services.cards import render_firing
from app.services.oncall import OncallRecipient, OncallTarget


@pytest.fixture(autouse=True)
def _config(tmp_path: Path) -> None:
    yaml = tmp_path / "cfg.yaml"
    yaml.write_text(Path(__file__).parents[3].joinpath("config", "example.yaml").read_text())
    set_config_path(yaml)


def make_alert() -> Alert:
    return Alert(
        incident_fingerprint="fp-card-mention",
        service="payment-api",
        severity="critical",
        summary="CPU > 95%",
        labels={},
        lark_message_id="om_x",
        state=AlertState.firing,
        created_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    )


def test_render_user_mention_from_oncall_target() -> None:
    target = OncallTarget(
        source="fd_schedule",
        recipients=(
            OncallRecipient(
                kind="user",
                email="bob@company.com",
                user_id="ou_bob",
                display_name="Bob",
            ),
        ),
    )

    payload = render_firing(make_alert(), oncall_target=target)

    # 飞书互动卡 lark_md 的 @ 用户语法：<at id=ou_xxx></at>（属性名 id、值不带引号、
    # 标签内为空，名字由飞书后端按 id 自动渲染）。
    assert "<at id=ou_bob></at>" in _all_content(payload)


def test_render_role_mention_from_fallback_target() -> None:
    target = OncallTarget(
        source="fallback_role",
        recipients=(OncallRecipient(kind="role", role="@on-call"),),
    )

    payload = render_firing(make_alert(), oncall_target=target)

    assert "@on-call" in _all_content(payload)


def test_render_multiple_user_mentions_from_oncall_target() -> None:
    target = OncallTarget(
        source="static_map",
        recipients=(
            OncallRecipient(
                kind="user",
                email="alice@company.com",
                user_id="ou_alice",
                display_name="Alice",
            ),
            OncallRecipient(
                kind="user",
                email="bob@company.com",
                user_id="ou_bob",
                display_name="Bob",
            ),
        ),
    )

    payload = render_firing(make_alert(), oncall_target=target)
    content = _all_content(payload)

    assert "<at id=ou_alice></at> <at id=ou_bob></at>" in content


def test_render_without_target_preserves_us1_no_mention_behavior() -> None:
    payload = render_firing(make_alert())
    flat = json.dumps(payload, ensure_ascii=False)

    assert "<at " not in flat
    assert "@on-call" not in flat


def test_render_firing_includes_silence_action_buttons() -> None:
    payload = render_firing(make_alert())
    flat = json.dumps(payload, ensure_ascii=False)
    # Lark v2 schema 要求 select_static option 的 value 是字符串（紧凑 JSON），
    # 所以这里先把包裹层 unescape 再做子串断言，确保结构化字段确实落进了序列化后的 payload。
    unescaped = flat.replace('\\"', '"')

    assert '"tag": "action"' not in flat  # schema v2 不再使用 action 容器
    assert '"tag": "select_static"' in flat
    assert '"element_id": "silence_select"' in flat
    assert "**⏱️ 静默时间**" in flat
    assert '"content": "选择静默时长"' in flat
    for duration in ["5min", "30min", "1h", "4h", "24h"]:
        assert f'"duration":"{duration}"' in unescaped
    assert '"kind":"silence"' in unescaped
    assert '"kind":"custom_open"' in unescaped
    assert '"alert_fingerprint":"fp-card-mention"' in unescaped


def test_render_firing_renders_dash_for_empty_oncall_target() -> None:
    """新模板：处理人员行总是渲染；空 oncall_target → 值显示 '-'。

    （旧行为是空 target 完全不渲染 On-call 区块；新模板字段表必须固定 6 行，
    所以语义改为 fallback "-"。）
    """
    target = OncallTarget(source="static_map", recipients=())

    payload = render_firing(make_alert(), oncall_target=target)
    flat = json.dumps(payload, ensure_ascii=False)

    assert "**On-call**" not in flat  # 旧的 On-call 标签已下线
    assert "处理人员" in flat
    assert "<at " not in flat  # 没有 recipients → 不应渲染任何 at 标签
    assert "处理人员**：-" in flat  # 模板固定 "**emoji label**：value" 形态


def _all_content(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False).replace('\\"', '"')
