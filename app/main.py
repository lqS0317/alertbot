"""FastAPI app factory + lifespan.

Phase 3 (US1) 增量：
  - 挂载 /webhook/fd 和 /webhook/lark 两条路由
  - lifespan 持有一个 LarkClient + MetaChannelReporter 实例（共享给所有请求）
  - 提供 build_app_for_tests() — 测试可注入自定义 transport 替换 Lark 出站
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response

from app import config as cfg_mod
from app import observability as obs
from app.clients.alertmanager import AlertmanagerClient
from app.clients.flashduty import FlashDutyClient
from app.clients.lark import LarkClient
from app.models import Base, make_engine, make_session_factory
from app.observability import MetaChannelReporter, MetricsRegistry, get_logger
from app.services.oncall import OncallResolver
from app.webhooks import alertmanager as am_webhook
from app.webhooks import flashduty as fd_webhook
from app.webhooks import lark as lark_webhook


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    obs.configure_structlog()
    log = get_logger("alertbot.main")

    # 配置文件 — 启动时由环境变量指定（已有的话别覆盖，给 build_app_for_tests 让步）。
    if cfg_mod._SNAPSHOT is None:
        config_path = Path(os.environ.get("ALERTBOT_CONFIG", "config/example.yaml")).resolve()
        cfg_mod.set_config_path(config_path)
        log.info("config_loaded", path=str(config_path))
    cfg = cfg_mod.get_config()

    # DB 引擎 — 同样优先用 app.state 已有的（测试场景）。
    if not hasattr(app.state, "session_factory"):
        database_url = getattr(
            app.state,
            "database_url",
            os.environ.get(
                "DATABASE_URL",
                "postgresql+asyncpg://alertbot:alertbot@localhost:5432/alertbot",
            ),
        )
        engine = make_engine(database_url)
        if getattr(app.state, "create_tables_on_startup", False):
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        app.state.session_factory = make_session_factory(engine)
        app.state._engine_owned = True
        log.info("db_engine_ready", database_url_kind=database_url.split("://", 1)[0])
    else:
        app.state._engine_owned = False

    # Lark 客户端 — 测试场景里 build_app_for_tests 已经塞了一个带 MockTransport 的进来。
    if not hasattr(app.state, "lark_client"):
        # 生产路径：用 app_id + app_secret 让 LarkClient 自己换 tenant_access_token
        # 并按 7200s TTL 自动续期；不要把 app_secret 当 Bearer 直接用。
        app_secret = os.environ.get(cfg.lark.app_secret_env, "")
        app.state.lark_client = LarkClient(app_id=cfg.lark.app_id, app_secret=app_secret)
        app.state._lark_owned = True
    else:
        app.state._lark_owned = False

    if not hasattr(app.state, "flashduty_client"):
        fd_token = os.environ.get(cfg.flashduty.schedule_api_token_env, "")
        app.state.flashduty_client = FlashDutyClient(
            base_url=cfg.flashduty.schedule_api_base,
            api_token=fd_token,
            cache_ttl_seconds=cfg.oncall.schedule_cache_ttl_seconds,
        )
        app.state._flashduty_owned = True
    else:
        app.state._flashduty_owned = False

    if not hasattr(app.state, "alertmanager_client"):
        am_token = os.environ.get(cfg.alertmanager.service_account_token_env, "")
        app.state.alertmanager_client = AlertmanagerClient(
            base_url=cfg.alertmanager.base_url,
            service_account_token=am_token,
            timeout_seconds=cfg.alertmanager.request_timeout_seconds,
        )

    # MetaChannelReporter — Phase 3 用 no-op，Phase 5 接到 Lark 群上报
    if not hasattr(app.state, "meta_reporter"):

        async def _noop_post(**_: object) -> None:
            return None

        app.state.meta_reporter = MetaChannelReporter(post_fn=_noop_post)

    if not hasattr(app.state, "oncall_resolver"):
        app.state.oncall_resolver = OncallResolver(
            flashduty=app.state.flashduty_client,
            lark=app.state.lark_client,
            reporter=app.state.meta_reporter,
        )

    # 配置热重载。测试 app 可关闭 watcher，避免 watchdog 底层 socket 在 pytest
    # unraisableexception 检查时触发 ResourceWarning。
    watcher = None
    if not getattr(app.state, "disable_hot_reload_watcher", False):
        watcher = cfg_mod.start_hot_reload_watcher(
            cfg_mod._PATH or Path("config/example.yaml"),
            reporter=app.state.meta_reporter,
        )
        log.info("hot_reload_watcher_started")

    try:
        yield
    finally:
        if watcher is not None:
            watcher.stop()
            watcher.join(timeout=2.0)
        if hasattr(app.state, "flashduty_client"):
            await app.state.flashduty_client.aclose()
        if hasattr(app.state, "alertmanager_client"):
            await app.state.alertmanager_client.aclose()
        if hasattr(app.state, "lark_client"):
            await app.state.lark_client.aclose()
        # AsyncEngine 不能直接 dispose；用 session_factory.kw 拿到 bind。
        engine = app.state.session_factory.kw.get("bind")
        if engine is not None:
            await engine.dispose()
        log.info("shutdown_complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="AlertBot",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.metrics = MetricsRegistry()

    @app.middleware("http")
    async def metrics_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        app.state.metrics.observe_route(str(route_path), request.method, elapsed)
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(
            content=app.state.metrics.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    app.include_router(fd_webhook.router)
    app.include_router(lark_webhook.router)
    app.include_router(am_webhook.router)
    return app


app = create_app()


# ───────────────────────── test-only factory ───────────────────────


def build_app_for_tests(
    *,
    database_url: str,
    lark_handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> FastAPI:
    """测试专用：构造一个 app + 注入 in-tmp DB + 可选 MockTransport-Lark。

    用法：
        app = build_app_for_tests(database_url="sqlite+aiosqlite:///x.db",
                                  lark_handler=lambda req: httpx.Response(200, json=...))
        with TestClient(app) as client: ...

    实现细节：把 session_factory + lark_client 提前塞到 app.state，lifespan 看到已存在
    就跳过自建步骤。这是为了让测试能彻底控制 Lark 出站行为。
    """
    app = create_app()
    app.state.disable_hot_reload_watcher = True
    app.state.database_url = database_url
    app.state.create_tables_on_startup = True

    if lark_handler is not None:
        transport = httpx.MockTransport(lark_handler)
        app.state.lark_client = LarkClient(transport=transport)
    app.state.alertmanager_client = AlertmanagerClient(
        base_url="http://am.test", transport=httpx.MockTransport(lambda _: httpx.Response(500))
    )
    # 默认测试 app 不启用 oncall resolver，避免老的 US1 集成测试被 label lookup 改变；
    # US2 单元测试直接覆盖 resolver / card mention 行为。
    app.state.oncall_resolver = None

    return app
