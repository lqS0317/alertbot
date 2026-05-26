"""按 alert.labels 路由到不同飞书群的单测。

覆盖：
  * first-match-wins：第一条命中即用，后面规则不再判断
  * 多 key match 是 AND：所有 key/value 都必须精确等于 labels
  * 不命中 → fallback 到 lark.group_chat_id
  * labels 缺少 match 中的 key → 该 route 不命中（不会误判为等于空串）
  * 路由 list 为空 / 未配置 → 总是走 group_chat_id（向后兼容）
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import reload_config, set_config_path
from app.services.cards import resolve_chat_id


def _write_cfg(tmp_path: Path, *, routes: list[dict] | None = None) -> None:
    base = yaml.safe_load(Path(__file__).parents[3].joinpath("config", "example.yaml").read_text())
    base["lark"]["group_chat_id"] = "oc_default"
    if routes is not None:
        base["lark"]["routes"] = routes
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True))
    set_config_path(path)
    reload_config()


@pytest.fixture(autouse=True)
def _config(tmp_path: Path) -> None:
    _write_cfg(tmp_path, routes=[])


def test_no_routes_returns_default_chat() -> None:
    assert resolve_chat_id({"cluster": "hsk-ops"}) == "oc_default"


def test_single_route_match_returns_route_chat(tmp_path: Path) -> None:
    _write_cfg(
        tmp_path,
        routes=[{"match": {"cluster": "hsk-ops-infra"}, "chat_id": "oc_ops"}],
    )

    assert resolve_chat_id({"cluster": "hsk-ops-infra"}) == "oc_ops"


def test_no_match_falls_back_to_default(tmp_path: Path) -> None:
    _write_cfg(
        tmp_path,
        routes=[{"match": {"cluster": "hsk-ops-infra"}, "chat_id": "oc_ops"}],
    )

    assert resolve_chat_id({"cluster": "hsk-prod"}) == "oc_default"


def test_first_match_wins(tmp_path: Path) -> None:
    """同时满足两条规则时，必须用前面那条；不能按后面规则"覆盖"。"""
    _write_cfg(
        tmp_path,
        routes=[
            {"match": {"env": "prod"}, "chat_id": "oc_prod"},
            {"match": {"env": "prod", "severity": "critical"}, "chat_id": "oc_prod_critical"},
        ],
    )

    # 即使能匹配第二条更精确的规则，按顺序也只命中第一条
    assert resolve_chat_id({"env": "prod", "severity": "critical"}) == "oc_prod"


def test_multi_key_match_is_and(tmp_path: Path) -> None:
    """match 里多个 key 是 AND 语义：缺一个都不命中。"""
    _write_cfg(
        tmp_path,
        routes=[
            {
                "match": {"env": "prod", "severity": "critical"},
                "chat_id": "oc_prod_critical",
            }
        ],
    )

    assert resolve_chat_id({"env": "prod", "severity": "critical"}) == "oc_prod_critical"
    # 只匹配 env，缺 severity → 不命中
    assert resolve_chat_id({"env": "prod"}) == "oc_default"
    # 只匹配 severity，缺 env → 不命中
    assert resolve_chat_id({"severity": "critical"}) == "oc_default"


def test_missing_label_key_does_not_falsely_match(tmp_path: Path) -> None:
    """labels 里完全没有 match 要求的 key → labels.get(k) 返回 None，不会等于
    规则中的 string；route 不应被错误命中。"""
    _write_cfg(
        tmp_path,
        routes=[{"match": {"cluster": ""}, "chat_id": "oc_unexpected"}],
    )

    assert resolve_chat_id({"severity": "warning"}) == "oc_default"


def test_empty_match_dict_rejected_by_schema(tmp_path: Path) -> None:
    """空 match 等价于"无条件匹配"，schema 必须拒绝防止误覆盖整个路由表。"""
    with pytest.raises(Exception):
        _write_cfg(
            tmp_path,
            routes=[{"match": {}, "chat_id": "oc_swallow_all"}],
        )


def test_route_order_supports_precedence_via_specificity(tmp_path: Path) -> None:
    """实际推荐用法：把更具体（key 多）/ 更优先的规则放前面。"""
    _write_cfg(
        tmp_path,
        routes=[
            {
                "match": {"env": "prod", "severity": "critical"},
                "chat_id": "oc_prod_critical",
            },
            {"match": {"env": "prod"}, "chat_id": "oc_prod"},
            {"match": {"severity": "critical"}, "chat_id": "oc_any_critical"},
        ],
    )

    assert resolve_chat_id({"env": "prod", "severity": "critical"}) == "oc_prod_critical"
    assert resolve_chat_id({"env": "prod", "severity": "warning"}) == "oc_prod"
    assert resolve_chat_id({"env": "ops", "severity": "critical"}) == "oc_any_critical"
    assert resolve_chat_id({"env": "ops", "severity": "warning"}) == "oc_default"
