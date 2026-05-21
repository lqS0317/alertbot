"""Layer 3 — Alertmanager `/api/v2/silences` client."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 1.0
EXCLUDED_MATCHER_KEYS = frozenset({"lark_user", "flashduty_team"})


class AlertmanagerAPIError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Alertmanager API error: HTTP {status_code} body={body[:200]}")
        self.status_code = status_code
        self.body = body


def matchers_from_labels(labels: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate alert labels to exact-match Alertmanager matchers."""
    matchers: list[dict[str, Any]] = []
    for key in sorted(labels):
        if key in EXCLUDED_MATCHER_KEYS:
            continue
        value = labels[key]
        if value is None:
            continue
        matchers.append({"name": key, "value": str(value), "isRegex": False, "isEqual": True})
    return matchers


class AlertmanagerClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_account_token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        headers = (
            {"Authorization": f"Bearer {service_account_token}"} if service_account_token else {}
        )
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_seconds, transport=transport, headers=headers
        )
        self._max_retries = max_retries

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_silence(
        self,
        *,
        matchers: list[dict[str, Any]],
        starts_at: datetime,
        ends_at: datetime,
        created_by: str,
        comment: str,
    ) -> str:
        body = {
            "matchers": matchers,
            "startsAt": starts_at.isoformat(),
            "endsAt": ends_at.isoformat(),
            "createdBy": created_by,
            "comment": comment,
        }
        resp = await self._send_with_retry("POST", "/api/v2/silences", json=body)
        data = resp.json()
        silence_id = data.get("silenceID") or data.get("silenceId") or data.get("id")
        if not isinstance(silence_id, str):
            raise AlertmanagerAPIError(resp.status_code, resp.text)
        return silence_id

    async def _send_with_retry(
        self, method: str, path: str, *, json: dict[str, Any]
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.request(method, path, json=json)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                await self._backoff(attempt)
                continue
            if 500 <= resp.status_code < 600:
                last_exc = AlertmanagerAPIError(resp.status_code, resp.text)
                await self._backoff(attempt)
                continue
            if resp.status_code >= 400:
                raise AlertmanagerAPIError(resp.status_code, resp.text)
            return resp
        if isinstance(last_exc, AlertmanagerAPIError):
            raise last_exc
        raise AlertmanagerAPIError(599, f"giving up after {self._max_retries}: {last_exc!r}")

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(RETRY_BACKOFF_BASE_SECONDS * (2**attempt))
