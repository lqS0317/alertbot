"""T011 — observability 测试：trace_id ContextVar、redact、MetaChannelReporter。

覆盖 spec FR-026 / FR-028 / Constitution VI（Fail Fast & Visible）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import observability as obs


def test_trace_id_default_is_dash() -> None:
    """未绑定时 trace_id 返回 '-'，方便日志渲染。"""
    obs.reset_trace_id_for_tests()
    assert obs.get_trace_id() == "-"


def test_trace_id_bind_and_get() -> None:
    obs.reset_trace_id_for_tests()
    token = obs.bind_trace_id("trace-X")
    try:
        assert obs.get_trace_id() == "trace-X"
    finally:
        obs.unbind_trace_id(token)
    assert obs.get_trace_id() == "-"


def test_new_trace_id_is_unique_and_short() -> None:
    a = obs.new_trace_id()
    b = obs.new_trace_id()
    assert a != b
    assert 8 <= len(a) <= 64


def test_redact_masks_known_secret_keys() -> None:
    payload = {
        "app_secret": "topsecret",
        "token": "abcd",
        "authorization": "Bearer xyz",
        "encrypt_key": "K",
        "summary": "fine to keep",
        "nested": {"token": "leak"},
    }
    redacted = obs.redact(payload)
    assert redacted["app_secret"] == "***"
    assert redacted["token"] == "***"
    assert redacted["authorization"] == "***"
    assert redacted["encrypt_key"] == "***"
    assert redacted["summary"] == "fine to keep"
    assert redacted["nested"]["token"] == "***"


def test_redact_does_not_mutate_input() -> None:
    payload = {"token": "leak"}
    redacted = obs.redact(payload)
    assert payload["token"] == "leak"  # original untouched
    assert redacted["token"] == "***"


def test_redact_truncates_fingerprint() -> None:
    payload = {"fingerprint": "a" * 200}
    redacted = obs.redact(payload)
    assert len(redacted["fingerprint"]) <= 64


@pytest.mark.asyncio
async def test_meta_channel_reporter_calls_lark_with_trace_id() -> None:
    """MetaChannelReporter.report() 必须把 trace_id 附在外发的 payload 里（FR-028）。"""
    obs.reset_trace_id_for_tests()
    obs.bind_trace_id("trace-meta")

    fake_post = AsyncMock(return_value=None)
    reporter = obs.MetaChannelReporter(post_fn=fake_post)
    await reporter.report("am.create_silence failed", details={"http_status": 502})

    fake_post.assert_awaited_once()
    args, kwargs = fake_post.await_args
    body = kwargs.get("body", args[0] if args else None)
    assert "trace-meta" in str(body)
    assert "am.create_silence" in str(body)


@pytest.mark.asyncio
async def test_meta_channel_reporter_silently_drops_on_send_failure() -> None:
    """报告自己失败不能阻断主流程（CP-VI 后半句）。"""
    fake_post = AsyncMock(side_effect=RuntimeError("network down"))
    reporter = obs.MetaChannelReporter(post_fn=fake_post)
    # MUST NOT raise:
    await reporter.report("anything", details={})
