"""单元测试：app.clients.alertmanager_inbound — payload 解析 + token 鉴权。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.clients.alertmanager_inbound import (
    TokenError,
    alert_to_event,
    dedup_key_for,
    parse_payload,
    verify_am_token,
)


def _alert(status: str = "firing", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": status,
        "labels": {
            "alertname": "HighCPU",
            "service": "payment-api",
            "severity": "critical",
        },
        "annotations": {"summary": "CPU > 95%"},
        "startsAt": "2026-05-25T06:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": "fp-001",
    }
    base.update(overrides)
    return base


def test_parse_payload_accepts_minimal_alert() -> None:
    body = json.dumps({"alerts": [_alert()]}).encode("utf-8")
    payload = parse_payload(body)
    assert len(payload.alerts) == 1
    assert payload.alerts[0].status == "firing"


def test_parse_payload_rejects_invalid_status() -> None:
    body = json.dumps({"alerts": [_alert(status="exploding")]}).encode("utf-8")
    with pytest.raises(ValidationError):
        parse_payload(body)


def test_parse_payload_rejects_invalid_json() -> None:
    with pytest.raises(ValidationError):
        parse_payload(b"not json")


def test_alert_to_event_maps_firing_to_incident_created() -> None:
    payload = parse_payload(json.dumps({"alerts": [_alert(status="firing")]}).encode("utf-8"))
    event = alert_to_event(payload.alerts[0])
    assert event.event_type == "incident.created"
    assert event.incident.fingerprint == "fp-001"
    assert event.incident.service == "payment-api"
    assert event.incident.severity == "critical"
    assert event.incident.summary == "CPU > 95%"


def test_alert_to_event_maps_resolved_to_incident_closed() -> None:
    payload = parse_payload(json.dumps({"alerts": [_alert(status="resolved")]}).encode("utf-8"))
    event = alert_to_event(payload.alerts[0])
    assert event.event_type == "incident.closed"


def test_alert_to_event_falls_back_to_alertname_when_service_missing() -> None:
    body = json.dumps(
        {"alerts": [_alert(labels={"alertname": "DiskFull", "severity": "warning"})]}
    ).encode("utf-8")
    payload = parse_payload(body)
    event = alert_to_event(payload.alerts[0])
    assert event.incident.service == "DiskFull"


def test_alert_to_event_default_severity_info_when_missing() -> None:
    body = json.dumps(
        {"alerts": [_alert(labels={"alertname": "X", "service": "svc"})]}
    ).encode("utf-8")
    payload = parse_payload(body)
    event = alert_to_event(payload.alerts[0])
    assert event.incident.severity == "info"


def test_dedup_key_uses_fingerprint_plus_status() -> None:
    payload = parse_payload(json.dumps({"alerts": [_alert(status="firing")]}).encode("utf-8"))
    assert dedup_key_for(payload.alerts[0]) == "fp-001:firing"


def test_dedup_key_falls_back_to_label_hash_when_fingerprint_missing() -> None:
    body = json.dumps(
        {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "X", "service": "y"},
                    "annotations": {},
                }
            ]
        }
    ).encode("utf-8")
    payload = parse_payload(body)
    key1 = dedup_key_for(payload.alerts[0])
    payload2 = parse_payload(body)
    key2 = dedup_key_for(payload2.alerts[0])
    assert key1 == key2
    assert key1.endswith(":firing")
    assert len(key1) > len(":firing")


def test_verify_am_token_accepts_correct_bearer() -> None:
    verify_am_token("Bearer abc123", "abc123")


def test_verify_am_token_rejects_missing_header() -> None:
    with pytest.raises(TokenError):
        verify_am_token(None, "abc123")


def test_verify_am_token_rejects_non_bearer_scheme() -> None:
    with pytest.raises(TokenError):
        verify_am_token("Basic abc123", "abc123")


def test_verify_am_token_rejects_empty_bearer() -> None:
    with pytest.raises(TokenError):
        verify_am_token("Bearer ", "abc123")


def test_verify_am_token_rejects_wrong_token() -> None:
    with pytest.raises(TokenError):
        verify_am_token("Bearer wrong", "abc123")


def test_verify_am_token_rejects_when_server_token_unconfigured() -> None:
    """safe-by-default：服务端 token 为空 → 全拒，避免无意中开放公网入口。"""
    with pytest.raises(TokenError):
        verify_am_token("Bearer anything", "")
