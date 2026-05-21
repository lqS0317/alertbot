"""Layer cross-cutting — structlog 配置 + trace_id ContextVar + redact + MetaChannelReporter。

Constitution VI（Fail Fast & Visible）：每一处 try/except 重抛点和每一次出站失败终态
必须经此模块上报到运维 meta-channel；上报自身失败不能阻断主流程。
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

import structlog

# ──────────────────────────── trace_id ──────────────────────────────

_TRACE_ID: ContextVar[str] = ContextVar("trace_id", default="-")


def get_trace_id() -> str:
    """读取当前请求/任务的 trace_id；未绑定时返 '-'."""
    return _TRACE_ID.get()


def bind_trace_id(value: str) -> Token[str]:
    """绑定 trace_id 到当前 ContextVar；返回 token 以便后续 reset。"""
    return _TRACE_ID.set(value)


def unbind_trace_id(token: Token[str]) -> None:
    """与 bind_trace_id 配对；middleware 在 finally 里调用。"""
    _TRACE_ID.reset(token)


def new_trace_id() -> str:
    """生成新的随机 trace_id（16 hex chars，约 64 位熵）。"""
    return secrets.token_hex(8)


def reset_trace_id_for_tests() -> None:
    """仅供测试：把当前 ContextVar 复位为默认值 '-'."""
    _TRACE_ID.set("-")


# ──────────────────────────── Prometheus-style metrics ──────────────


@dataclass
class MetricsRegistry:
    """Tiny Prometheus text registry.

    这里避免引入额外依赖，足够覆盖 Phase 7 要求的 5 个序列。
    """

    route_durations: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    idempotency_dedup_total: dict[str, int] = field(default_factory=dict)
    silence_real_email_total: int = 0
    silence_lark_id_total: int = 0
    meta_channel_report_latencies: list[float] = field(default_factory=list)

    def observe_route(self, route: str, method: str, seconds: float) -> None:
        self.route_durations.setdefault((route, method), []).append(seconds)

    def inc_dedup(self, event_source: str) -> None:
        self.idempotency_dedup_total[event_source] = (
            self.idempotency_dedup_total.get(event_source, 0) + 1
        )

    def inc_silence_created_by(self, created_by: str) -> None:
        if created_by.startswith("lark:"):
            self.silence_lark_id_total += 1
        else:
            self.silence_real_email_total += 1

    def observe_meta_report(self, seconds: float) -> None:
        self.meta_channel_report_latencies.append(seconds)

    def render_prometheus(self) -> str:
        lines = [
            "# HELP webhook_handler_duration_seconds Webhook/HTTP route duration.",
            "# TYPE webhook_handler_duration_seconds summary",
        ]
        for (route, method), values in sorted(self.route_durations.items()):
            count = len(values)
            total = sum(values)
            lines.append(
                f'webhook_handler_duration_seconds_count{{route="{route}",method="{method}"}} {count}'
            )
            lines.append(
                f'webhook_handler_duration_seconds_sum{{route="{route}",method="{method}"}} {total:.6f}'
            )

        lines.extend(
            [
                "# HELP idempotency_dedup_total Duplicate webhook events short-circuited.",
                "# TYPE idempotency_dedup_total counter",
            ]
        )
        if not self.idempotency_dedup_total:
            lines.append('idempotency_dedup_total{event_source="none"} 0')
        for source, count in sorted(self.idempotency_dedup_total.items()):
            lines.append(f'idempotency_dedup_total{{event_source="{source}"}} {count}')

        lines.extend(
            [
                "# HELP silence_created_by_real_email_total Silences with email createdBy.",
                "# TYPE silence_created_by_real_email_total counter",
                f"silence_created_by_real_email_total {self.silence_real_email_total}",
                "# HELP silence_created_by_lark_id_total Silences with lark:<user_id> fallback.",
                "# TYPE silence_created_by_lark_id_total counter",
                f"silence_created_by_lark_id_total {self.silence_lark_id_total}",
                "# HELP meta_channel_report_latency_seconds Meta-channel report latency.",
                "# TYPE meta_channel_report_latency_seconds summary",
                f"meta_channel_report_latency_seconds_count {len(self.meta_channel_report_latencies)}",
                f"meta_channel_report_latency_seconds_sum {sum(self.meta_channel_report_latencies):.6f}",
            ]
        )
        return "\n".join(lines) + "\n"


# ──────────────────────────── structlog ─────────────────────────────


def _add_trace_id(
    _: Any, __: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event_dict["trace_id"] = get_trace_id()
    return event_dict


def configure_structlog() -> None:
    """生产用 JSON-on-stdout；开发可在 main.py 里覆盖渲染器。

    processor 顺序参考 research.md §10。
    """
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_trace_id,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO+
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


# ──────────────────────────── redact ────────────────────────────────

# 任何 key 命中以下集合都会被 mask 为 "***"。
# 这是 spec FR-025 / Constitution Security 的最小实现：审计入库前不能含 secret。
_SENSITIVE_KEYS = frozenset(
    {
        "app_secret",
        "token",
        "authorization",
        "auth",
        "encrypt_key",
        "verification_token",
        "webhook_secret",
        "service_account_token",
        "api_token",
        "password",
        "secret",
        "private_key",
    }
)
# fingerprint 不是 secret 但可能极长；截断到 64 字符避免审计表撑爆。
_TRUNCATE_KEYS = frozenset({"fingerprint", "summary"})
_TRUNCATE_LIMIT = 64


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    """递归地把敏感字段替换为 '***'，把过长字段截断；不修改原 dict。"""

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS:
                    out[k] = "***"
                elif isinstance(k, str) and k.lower() in _TRUNCATE_KEYS and isinstance(v, str):
                    out[k] = v[:_TRUNCATE_LIMIT]
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(node, list):
            return [_walk(x) for x in node]
        return node

    walked = _walk(payload)
    assert isinstance(walked, dict)  # payload 顶层一定是 dict
    return walked


# ──────────────────────────── MetaChannelReporter ───────────────────

PostFn = Callable[..., Awaitable[None]]


class MetaChannelReporter:
    """异步上报运维 meta-channel（一个独立 Lark 群）。

    设计要点（CP-VI）：
      - 上报自身失败不能阻断主流程 — 一律 swallow + log.warning。
      - 注入 PostFn 而非直接依赖 clients/lark.py，保持 cross-cutting 不反向依赖业务层。
      - body 里强制带 trace_id，便于链路串联（FR-028）。
    """

    def __init__(self, post_fn: PostFn) -> None:
        self._post = post_fn
        self._log = get_logger("alertbot.observability.meta")

    async def report(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        body = {
            "trace_id": get_trace_id(),
            "message": message,
            "details": redact(details or {}),
        }
        try:
            await self._post(body=body)
        except Exception as exc:  # pragma: no cover - 容错路径，单测有显式覆盖
            # 不向上抛 — 上报失败不能反过来打死业务（CP-VI 第二段）。
            self._log.warning(
                "meta_channel_report_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                message=message,
            )
