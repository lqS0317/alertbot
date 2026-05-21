"""Cross-cutting — YAML 配置加载 + Pydantic v2 校验 + watchdog 热重载（snapshot 原子替换）。

设计原则（spec FR-029 / Constitution V）：
  - 业务字面量（颜色、TTL、按钮、邮箱映射、时区、24h 上限）只能来自这里，禁止硬编码。
  - 配置变更不需要重启服务、不需要发新版本。
  - 校验失败时保留旧 snapshot，并由 watcher 上报到 meta-channel（CP-VI）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from app.observability import MetaChannelReporter, get_logger

_log = get_logger("alertbot.config")


# ───────────────────────── Pydantic schema ─────────────────────────


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LarkConfig(_Frozen):
    app_id: str
    app_secret_env: str
    encrypt_key_env: str
    verification_token_env: str
    group_chat_id: str
    meta_channel_id: str


class FlashdutyConfig(_Frozen):
    webhook_secret_env: str
    schedule_api_base: str
    schedule_api_token_env: str


class AlertmanagerConfig(_Frozen):
    base_url: str
    service_account_token_env: str
    request_timeout_seconds: int = Field(default=5, ge=1, le=30)


OncallTier = Literal["incident_label", "fd_schedule", "static_map", "fallback_role"]


class OncallConfig(_Frozen):
    priority_chain: list[OncallTier]
    static_service_map: dict[str, str] = Field(default_factory=dict)
    fallback_role: str
    schedule_cache_ttl_seconds: int = Field(default=300, ge=0, le=300)


SilenceDuration = Literal["5min", "30min", "1h", "4h", "24h"]


def _default_silence_durations() -> list[SilenceDuration]:
    return ["5min", "30min", "1h", "4h", "24h"]


class SilenceButtonsConfig(_Frozen):
    fixed_durations: list[SilenceDuration] = Field(default_factory=_default_silence_durations)
    enable_custom: bool = True


class AlertBotConfig(_Frozen):
    lark: LarkConfig
    flashduty: FlashdutyConfig
    alertmanager: AlertmanagerConfig
    oncall: OncallConfig
    severity_colors: dict[str, str]
    silence_buttons: SilenceButtonsConfig
    timezone: str
    max_silence_hours: int = Field(default=24, ge=1, le=24)


# ───────────────────────── loader ──────────────────────────────────


def load_config_from_yaml(path: Path) -> AlertBotConfig:
    """直接从 YAML 文件加载并校验。失败抛 ValidationError / yaml.YAMLError。"""
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return AlertBotConfig.model_validate(raw)


# ───────────────────────── snapshot singleton ──────────────────────

_LOCK = threading.RLock()
_SNAPSHOT: AlertBotConfig | None = None
_PATH: Path | None = None


def set_config_path(path: Path) -> None:
    """用于启动 / 测试：指向 YAML 文件并立即加载首版 snapshot。"""
    global _PATH, _SNAPSHOT
    with _LOCK:
        _PATH = path
        _SNAPSHOT = load_config_from_yaml(path)


def get_config() -> AlertBotConfig:
    """返回当前 snapshot；任何业务调用都走这一条路。"""
    with _LOCK:
        if _SNAPSHOT is None:
            raise RuntimeError("config not loaded — call set_config_path() first")
        return _SNAPSHOT


def reload_config() -> AlertBotConfig:
    """显式重载（hot-reload watcher 在收到 modify 事件后调用）。

    校验失败时保留旧 snapshot 不变，并把异常上抛 — 由 watcher 的回调上报 meta-channel。
    """
    global _SNAPSHOT
    with _LOCK:
        if _PATH is None:
            raise RuntimeError("config path not set")
        new_snapshot = load_config_from_yaml(_PATH)  # 抛异常 → 旧 snapshot 不动
        _SNAPSHOT = new_snapshot
        return new_snapshot


def _reset_for_tests() -> None:
    """仅供测试 — 复位 module-level 单例，防测试间串扰。"""
    global _SNAPSHOT, _PATH
    with _LOCK:
        _SNAPSHOT = None
        _PATH = None


# ───────────────────────── watchdog hot-reload ─────────────────────


class _ReloadHandler(FileSystemEventHandler):
    """监听配置文件 modify 事件 → 同步重载。

    K8s ConfigMap 通过 symlink 旋转，所以也监听 created/moved 事件。
    """

    def __init__(self, target_path: Path, reporter: MetaChannelReporter | None) -> None:
        self._target = target_path.resolve()
        self._reporter = reporter

    def on_any_event(self, event: FileSystemEvent) -> None:
        # 只关心目标文件本身（事件路径或目录被改写都算）。
        raw = event.src_path
        src_str = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        src = Path(src_str).resolve() if src_str else None
        if src and src != self._target and src.parent != self._target.parent:
            return
        if event.event_type not in {"modified", "created", "moved"}:
            return
        self._safe_reload()

    def _safe_reload(self) -> None:
        try:
            new_cfg = reload_config()
            _log.info(
                "config_reloaded",
                timezone=new_cfg.timezone,
                max_silence_hours=new_cfg.max_silence_hours,
            )
        except Exception as exc:
            _log.error(
                "config_reload_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            if self._reporter is not None:
                # fire-and-forget；reporter 自己 swallow 失败
                import asyncio

                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    return
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._reporter.report(
                            "config_reload_failed",
                            details={"error": str(exc), "type": type(exc).__name__},
                        ),
                        loop,
                    )


def start_hot_reload_watcher(
    path: Path,
    reporter: MetaChannelReporter | None = None,
) -> BaseObserver:
    """启动文件变更观察者。返回 observer，调用方负责 stop()/join()."""
    observer: BaseObserver = Observer()
    handler = _ReloadHandler(path, reporter)
    observer.schedule(handler, str(path.parent), recursive=False)
    observer.start()
    return observer
