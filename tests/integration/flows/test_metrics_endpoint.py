"""T091/T092 — Prometheus metrics endpoint and request-duration instrumentation."""

from __future__ import annotations

from collections.abc import Callable

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_metrics_endpoint_exposes_required_series(
    fastapi_app_factory: Callable[..., FastAPI],
) -> None:
    app = fastapi_app_factory(lark_handler=lambda _: httpx.Response(200))

    with TestClient(app) as client:
        client.get("/healthz")
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "webhook_handler_duration_seconds" in body
    assert "idempotency_dedup_total" in body
    assert "silence_created_by_real_email_total" in body
    assert "silence_created_by_lark_id_total" in body
    assert "meta_channel_report_latency_seconds" in body


def test_metrics_endpoint_records_healthz_route_duration(
    fastapi_app_factory: Callable[..., FastAPI],
) -> None:
    app = fastapi_app_factory(lark_handler=lambda _: httpx.Response(200))

    with TestClient(app) as client:
        client.get("/healthz")
        body = client.get("/metrics").text

    assert 'route="/healthz"' in body
