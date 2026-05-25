"""Shared pytest fixtures (T025).

提供：
- in-memory SQLite AsyncSession（每个测试一个干净的 schema）
- httpx.MockTransport 工厂
- 冻结时间（freezegun，按需 import）
- meta-channel reporter 假对象
- app_factory：构造一个挂好测试 DB + 测试配置 + MockTransport-Lark 的 FastAPI app
"""

from __future__ import annotations

import textwrap
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import config as cfg_mod
from app.models import Base

# ───────────────────────── DB session ────────────────────────────────


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """每个测试一个全新的 in-memory SQLite + 三表建好。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session
    await engine.dispose()


# ───────────────────────── MockTransport 工厂 ─────────────────────────


@pytest.fixture
def mock_transport() -> Callable[[Callable[[httpx.Request], httpx.Response]], httpx.MockTransport]:
    """传一个 handler(callable) → 返 MockTransport，可注入到 httpx.AsyncClient(transport=…)."""

    def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    return _factory


@pytest.fixture
def mock_meta_reporter() -> AsyncMock:
    reporter = AsyncMock()
    reporter.report = AsyncMock(return_value=None)
    return reporter


# ───────────────────────── 测试配置 + app_factory ────────────────────


_TEST_YAML = textwrap.dedent(
    """
    lark:
      app_id: "cli_test"
      app_secret_env: "TEST_LARK_APP_SECRET"
      encrypt_key_env: "TEST_LARK_ENCRYPT_KEY"
      verification_token_env: "TEST_LARK_VERIFY_TOKEN"
      group_chat_id: "oc_test_group"
      meta_channel_id: "oc_test_meta"
    flashduty:
      webhook_secret_env: "TEST_FD_SECRET"
      schedule_api_base: "https://api.flashcat.test/api/v1"
      schedule_api_token_env: "TEST_FD_TOKEN"
    alertmanager:
      base_url: "http://am.test:9093"
      service_account_token_env: "TEST_AM_TOKEN"
      request_timeout_seconds: 5
      webhook_token_env: "TEST_AM_WEBHOOK_TOKEN"
    oncall:
      priority_chain: [incident_label, fd_schedule, static_map, fallback_role]
      incident_label_key: "lark_user"
      static_service_map:
        payment-api: ["alice@company.com"]
      fallback_role: ["@on-call"]
      schedule_cache_ttl_seconds: 60
    severity_colors:
      critical: red
      warning: orange
      info: blue
    silence_buttons:
      fixed_durations: [5min, 30min, 1h, 4h, 24h]
      enable_custom: true
    timezone: "Asia/Shanghai"
    max_silence_hours: 24
    """
).strip()


# Phase-3 测试用的 secret 环境变量（必填以便 verify_signature 取值）
_FD_SECRET_VALUE = "fd-test-secret-shared"
_AM_WEBHOOK_TOKEN_VALUE = "am-test-token-shared"


@pytest.fixture
def fd_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("TEST_FD_SECRET", _FD_SECRET_VALUE)
    return _FD_SECRET_VALUE


@pytest.fixture
def am_webhook_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """把入站 Alertmanager webhook 的共享 token 注入到 TEST_AM_WEBHOOK_TOKEN。

    与 _TEST_YAML 里的 alertmanager.webhook_token_env 对齐。
    """
    monkeypatch.setenv("TEST_AM_WEBHOOK_TOKEN", _AM_WEBHOOK_TOKEN_VALUE)
    return _AM_WEBHOOK_TOKEN_VALUE


@pytest.fixture
def write_test_config(tmp_path: Path) -> Path:
    """把 _TEST_YAML 写到 tmp 文件并指向它，返路径。"""
    cfg_mod._reset_for_tests()
    p = tmp_path / "alertbot-test.yaml"
    p.write_text(_TEST_YAML)
    cfg_mod.set_config_path(p)
    return p


@pytest.fixture
def fastapi_app_factory(
    tmp_path: Path,
    write_test_config: Path,
    fd_secret: str,
) -> Iterator[Callable[..., FastAPI]]:
    """构造一个 lifespan 已启动好的 FastAPI 实例，用 MockTransport 注入 Lark 出站。

    使用例：
        def handler(req): return httpx.Response(200, json={"data": {"message_id": "msg-1"}})
        with TestClient(factory(lark_handler=handler)) as c:
            ...
    """
    from app.main import build_app_for_tests  # 延迟 import，避免循环

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    def _factory(
        *,
        lark_handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> FastAPI:
        return build_app_for_tests(database_url=db_url, lark_handler=lark_handler)

    yield _factory
    cfg_mod._reset_for_tests()


@pytest.fixture
def sample_audit_payload() -> dict[str, Any]:
    return {
        "trace_id": "trace-test-0001",
        "event_source": "flashduty",
        "dedup_key": "fp-001:created",
        "operation": "webhook.fd.received",
        "actor_lark_user_id": None,
        "actor_email_redacted": None,
        "payload_redacted": {"summary": "synthetic"},
        "result": "success",
        "result_summary": "ok",
    }
