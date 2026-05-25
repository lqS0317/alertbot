"""Layer 3 — Lark Open Platform 客户端：互动卡片 post / patch。

US1 阶段只用 post_card / patch_card；US2 加 lookup_user_email；US3 加 form modal +
encrypted-payload signature。

设计：
- httpx.AsyncClient 注入式 — 测试可用 MockTransport 替换底层 transport，无需 monkey-patch。
- 5s 超时 + 3 次指数退避重试 (1s/2s/4s)，仅在 timeout/connect-error/5xx 时重试 (FR-027)。
- 404 on PATCH → 抛 MessageNotFound，由 services.cards 层走 FR-011 fallback。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import BaseModel, ConfigDict, Field

POST_CARD_PATH = "/open-apis/im/v1/messages?receive_id_type=chat_id"
PATCH_CARD_PATH_TPL = "/open-apis/im/v1/messages/{message_id}"
USER_BY_ID_PATH_TPL = "/open-apis/contact/v3/users/{user_id}"
USER_BATCH_GET_ID_PATH = "/open-apis/contact/v3/users/batch_get_id"
FORM_MODAL_PATH = "/open-apis/im/v1/cards/forms"
TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"

DEFAULT_BASE_URL = "https://open.feishu.cn"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 1.0
LARK_SIGNATURE_REPLAY_WINDOW_SECONDS = 300
# tenant_access_token 默认有效期约 7200s；提前 60s 主动续，避免临界过期。
TENANT_TOKEN_REFRESH_LEAD_SECONDS = 60
TENANT_TOKEN_DEFAULT_EXPIRE_SECONDS = 7200


class LarkAPIError(Exception):
    """非 200 / Lark business-code 非零 — 调用方可决定是 meta-channel 上报还是降级。"""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Lark API error: HTTP {status_code} body={body}")
        self.status_code = status_code
        self.body = body


class MessageNotFoundError(LarkAPIError):
    """patch_card 收到 404 — 调用方应走 FR-011 fallback (重发新卡 + meta-channel 报告)。"""


class LarkSignatureError(Exception):
    """Lark 入站签名/时间窗/解密失败。"""


@dataclass(frozen=True)
class LarkActionEvent:
    event_id: str
    operator_user_id: str
    kind: str
    alert_fingerprint: str | None
    duration: str | None


class _LarkActionHeader(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event_id: str
    event_type: str


class _LarkOperator(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str | None = None
    open_id: str | None = None


class _LarkAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    value: dict[str, Any] = Field(default_factory=dict)


class _LarkActionPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    header: _LarkActionHeader
    event: dict[str, Any]


class LarkClient:
    """thin async wrapper. 持有一个 AsyncClient 实例（可注入 transport for tests）。

    本类不读 config — caller 负责把 base_url/token 传进来；这样：
      1) 单元测试可以构造 LarkClient(base_url="http://mock", transport=MockTransport(...))
      2) 不会反向依赖 app/config.py 在 layer 4 之外的任何点
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        tenant_token: str = "",
        app_id: str = "",
        app_secret: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        """两种鉴权模式：

        - 显式 `tenant_token`：直接当 Bearer 用（测试 / 临时调试）。
        - `app_id` + `app_secret`：在首次发请求前用 `/auth/v3/tenant_access_token/internal`
          换取 tenant_access_token 并缓存，过期前 60s 自动续。这是生产唯一推荐路径。

        两个都给时优先使用 app_id+app_secret，因为 tenant_token 一定会过期。
        """
        self._base_url = base_url
        self._app_id = app_id
        self._app_secret = app_secret
        self._tenant_token = tenant_token
        # 静态 token：当作永不过期；动态模式下 _ensure_token 首次调用会真正赋值。
        self._tenant_token_expires_at = float("inf") if tenant_token and not app_id else 0.0
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
        )
        self._email_by_user_id: dict[str, str | None] = {}
        self._user_by_email: dict[str, tuple[str, str] | None] = {}
        self._token_lock = asyncio.Lock()

    def _auth_headers(self) -> dict[str, str]:
        if self._tenant_token:
            return {"Authorization": f"Bearer {self._tenant_token}"}
        return {}

    async def _ensure_tenant_token(self) -> None:
        """动态模式：缺 token 或快过期时拿 app_id/app_secret 换一次。"""
        if not (self._app_id and self._app_secret):
            # 静态模式 / 测试 mock — 由调用方保证 _tenant_token 已可用或不需要 auth。
            return
        if self._tenant_token and time.time() < self._tenant_token_expires_at:
            return
        async with self._token_lock:
            if self._tenant_token and time.time() < self._tenant_token_expires_at:
                return
            resp = await self._client.post(
                TENANT_TOKEN_PATH,
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            if resp.status_code != 200:
                raise LarkAPIError(resp.status_code, resp.text)
            data = resp.json()
            if data.get("code", 0) != 0 or not isinstance(data.get("tenant_access_token"), str):
                raise LarkAPIError(resp.status_code, resp.text)
            new_token = str(data["tenant_access_token"])
            expire_seconds = data.get("expire", TENANT_TOKEN_DEFAULT_EXPIRE_SECONDS)
            try:
                ttl = max(60, int(expire_seconds) - TENANT_TOKEN_REFRESH_LEAD_SECONDS)
            except (TypeError, ValueError):
                ttl = TENANT_TOKEN_DEFAULT_EXPIRE_SECONDS - TENANT_TOKEN_REFRESH_LEAD_SECONDS
            self._tenant_token = new_token
            self._tenant_token_expires_at = time.time() + ttl

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post_card(self, *, chat_id: str, card_payload: dict[str, Any]) -> str:
        """POST 新互动卡片 → 返回 message_id。"""
        body = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card_payload, ensure_ascii=False),
        }
        resp = await self._send_with_retry("POST", POST_CARD_PATH, json=body)
        data = resp.json()
        # Lark 业务码 0 = 成功
        if data.get("code", 0) != 0:
            raise LarkAPIError(resp.status_code, resp.text)
        msg_id = data.get("data", {}).get("message_id")
        if not isinstance(msg_id, str):
            raise LarkAPIError(resp.status_code, f"missing data.message_id in {data}")
        return msg_id

    async def patch_card(self, *, message_id: str, card_payload: dict[str, Any]) -> None:
        """PATCH 已有卡片（同一 message_id）。404 → MessageNotFound（FR-011）。"""
        path = PATCH_CARD_PATH_TPL.format(message_id=message_id)
        body = {"content": json.dumps(card_payload, ensure_ascii=False)}
        try:
            resp = await self._send_with_retry("PATCH", path, json=body)
        except LarkAPIError as exc:
            if exc.status_code == 404:
                raise MessageNotFoundError(404, exc.body) from exc
            raise
        data = resp.json()
        if data.get("code", 0) != 0:
            raise LarkAPIError(resp.status_code, resp.text)

    async def lookup_user_email(self, user_id: str) -> str | None:
        """Lark user_id/open_id → email。结果缓存（FR-015/018 前置能力）。"""
        if user_id in self._email_by_user_id:
            return self._email_by_user_id[user_id]

        resp = await self._send_with_retry(
            "GET",
            USER_BY_ID_PATH_TPL.format(user_id=user_id),
            params={"user_id_type": "open_id"},
        )
        data = resp.json()
        if data.get("code", 0) != 0:
            if data.get("code") == 404:
                self._email_by_user_id[user_id] = None
                return None
            raise LarkAPIError(resp.status_code, resp.text)
        email = _extract_email(data)
        self._email_by_user_id[user_id] = email
        return email

    async def lookup_user_by_email(self, email: str) -> tuple[str, str] | None:
        """email → (user_id, display_name)，用于卡片 @-mention 渲染。"""
        if email in self._user_by_email:
            return self._user_by_email[email]

        # 飞书 batch_get_id 是 POST JSON body；用 GET 会被路由成 `/users/{open_id}`，
        # 进而把 `batch_get_id` 当成 open_id 报 99992351。
        try:
            resp = await self._send_with_retry(
                "POST", USER_BATCH_GET_ID_PATH, json={"emails": [email]}
            )
        except LarkAPIError:
            # 通讯录权限缺失 / 用户不存在等不应阻断告警卡片；调用方会退化展示邮箱文本。
            self._user_by_email[email] = None
            return None
        data = resp.json()
        if data.get("code", 0) != 0:
            self._user_by_email[email] = None
            return None
        user = _extract_user_by_email(data, email)
        self._user_by_email[email] = user
        return user

    async def open_form_modal(
        self,
        *,
        alert_fingerprint: str,
        operator_user_id: str,
        field: str = "duration",
    ) -> None:
        """Open a Lark form modal for custom silence duration (US4)."""
        body = {
            "alert_fingerprint": alert_fingerprint,
            "operator_user_id": operator_user_id,
            "field": field,
            "title": "Custom silence duration",
            "placeholder": "e.g. 7h or 45min (max 24h)",
        }
        resp = await self._send_with_retry("POST", FORM_MODAL_PATH, json=body)
        data = resp.json()
        if data.get("code", 0) != 0:
            raise LarkAPIError(resp.status_code, resp.text)

    async def _send_with_retry(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """超时 + 5xx + 网络错误最多 3 次重试，1s→2s→4s + 25% 抖动。

        4xx（除 404 走特殊路径）不重试 — 那是调用方的 bug。
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                await self._ensure_tenant_token()
                resp = await self._client.request(
                    method, path, json=json, params=params, headers=self._auth_headers()
                )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                await self._backoff(attempt)
                continue

            if 500 <= resp.status_code < 600:
                last_exc = LarkAPIError(resp.status_code, resp.text)
                await self._backoff(attempt)
                continue

            if resp.status_code >= 400:
                # 4xx：直接抛，让调用方按场景区分（404 → MessageNotFound）。
                raise LarkAPIError(resp.status_code, resp.text)

            return resp

        # 三次都失败
        if isinstance(last_exc, LarkAPIError):
            raise last_exc
        raise LarkAPIError(599, f"giving up after {self._max_retries} retries: {last_exc!r}")

    async def _backoff(self, attempt: int) -> None:
        base = RETRY_BACKOFF_BASE_SECONDS * (2**attempt)
        jitter = base * 0.25 * (random.random() * 2 - 1)
        await asyncio.sleep(max(0.0, base + jitter))


def verify_lark_signature(
    *,
    secret: str,
    body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    nonce_header: str | None,
    now: int | None = None,
) -> None:
    if not signature_header or not timestamp_header or not nonce_header:
        raise LarkSignatureError("missing signature headers")
    try:
        ts = int(timestamp_header)
    except ValueError as exc:
        raise LarkSignatureError("timestamp not an integer") from exc
    current = now if now is not None else int(time.time())
    if abs(current - ts) > LARK_SIGNATURE_REPLAY_WINDOW_SECONDS:
        raise LarkSignatureError("timestamp outside replay window")
    msg = f"{timestamp_header}{nonce_header}".encode() + body
    expected = base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(signature_header, expected):
        raise LarkSignatureError("signature mismatch")


def decrypt_lark_body_if_needed(*, encrypt_key: str, body: bytes) -> bytes:
    """If body is `{"encrypt": "..."}`, decrypt AES-CBC with sha256(encrypt_key)."""
    raw = json.loads(body.decode("utf-8"))
    if not isinstance(raw, dict) or "encrypt" not in raw:
        return body
    encrypted = base64.b64decode(str(raw["encrypt"]))
    key = hashlib.sha256(encrypt_key.encode()).digest()
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    padded = cipher.decryptor().update(encrypted) + cipher.decryptor().finalize()
    pad = padded[-1]
    if pad < 1 or pad > 16:
        raise LarkSignatureError("invalid encrypted padding")
    return padded[:-pad]


def parse_lark_action_event(payload: dict[str, Any]) -> LarkActionEvent:
    parsed = _LarkActionPayload.model_validate(payload)
    if parsed.header.event_type != "card.action.trigger":
        raise ValueError(f"unsupported Lark event_type: {parsed.header.event_type}")
    operator = _LarkOperator.model_validate(parsed.event.get("operator", {}))
    action = _LarkAction.model_validate(parsed.event.get("action", {}))
    user_id = operator.user_id or operator.open_id
    if not user_id:
        raise ValueError("operator.user_id/open_id missing")
    value = action.value
    return LarkActionEvent(
        event_id=parsed.header.event_id,
        operator_user_id=user_id,
        kind=str(value.get("kind", "")),
        alert_fingerprint=(
            value.get("alert_fingerprint")
            if isinstance(value.get("alert_fingerprint"), str)
            else None
        ),
        duration=value.get("duration") if isinstance(value.get("duration"), str) else None,
    )


def _extract_email(data: dict[str, Any]) -> str | None:
    user = data.get("data", {}).get("user") if isinstance(data.get("data"), dict) else None
    if isinstance(user, dict):
        email = user.get("email") or user.get("enterprise_email")
        if isinstance(email, str) and email:
            return email
    return None


def _extract_user_by_email(data: dict[str, Any], email: str) -> tuple[str, str] | None:
    body = data.get("data")
    if not isinstance(body, dict):
        return None
    users = body.get("user_list") or body.get("users") or body.get("items")
    if isinstance(users, list):
        for item in users:
            if not isinstance(item, dict):
                continue
            item_email = item.get("email") or item.get("enterprise_email")
            if item_email != email:
                continue
            user_id = item.get("user_id") or item.get("open_id")
            name = item.get("name") or item.get("display_name") or email
            if isinstance(user_id, str) and isinstance(name, str):
                return user_id, name
    # 部分 Lark API 返回 email -> user_id map
    email_users = body.get("email_users")
    if isinstance(email_users, dict):
        item = email_users.get(email)
        if isinstance(item, dict):
            user_id = item.get("user_id") or item.get("open_id")
            name = item.get("name") or item.get("display_name") or email
            if isinstance(user_id, str) and isinstance(name, str):
                return user_id, name
    return None
