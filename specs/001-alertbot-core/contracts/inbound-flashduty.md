# Inbound Contract — FlashDuty Webhook (`POST /webhook/fd`)

**Counterparty**: FlashDuty (`fd-app.flashcat.cloud` or self-hosted equivalent).
**Owner module**: `app/webhooks/flashduty.py`.
**Constitution gates**: I (webhook-first), II (idempotent), VII (verify), IV (audit).
**Spec FRs**: FR-001 / FR-002 / FR-005 / FR-021.

## Request

| Item | Value |
|---|---|
| Method | `POST` |
| Path | `/webhook/fd` |
| Content-Type | `application/json; charset=utf-8` |
| Signature header | `X-FD-Signature: sha256=<hex(hmac_sha256(secret, ts || '.' || body))>` |
| Timestamp header | `X-FD-Timestamp: <unix-seconds>` |
| Replay window | timestamps older than 5 minutes are rejected with 401 |
| Body | JSON envelope, see below |

> **Note**: the exact FlashDuty signature header names and HMAC scheme are confirmed
> in [research.md §1](../research.md#1-flashduty-webhook-signature-scheme). If
> FlashDuty changes the scheme, only `app/webhooks/flashduty.py::verify_signature`
> needs to change; the contract is otherwise identical.

### Body envelope (illustrative; canonical fixtures in `tests/fixtures/flashduty/`)

```json
{
  "event_id": "fd-evt-7c3a91…",
  "event_type": "incident.created",
  "timestamp": 1746601234,
  "incident": {
    "fingerprint": "alertname=HighCPU,instance=web-01",
    "service": "payment-api",
    "severity": "critical",
    "summary": "CPU > 95% for 5m on web-01",
    "labels": {
      "alertname": "HighCPU",
      "instance": "web-01",
      "lark_user": "alice@company.com"
    },
    "started_at": "2026-05-07T08:00:00Z"
  }
}
```

`event_type` ∈ `{ incident.created, incident.updated, incident.closed }`.

## Processing pipeline (in `app/webhooks/flashduty.py`)

```
1. Read X-FD-Signature + X-FD-Timestamp
2. Reject if |now - timestamp| > 300s     → 401
3. Re-compute HMAC; constant-time compare → 401 on mismatch       (CP-VII)
4. Parse body (Pydantic FlashDutyEvent model)
5. INSERT INTO audit_log (event_source='flashduty',
                          dedup_key='<fingerprint>:<event_type>',
                          operation='webhook.fd.received',
                          payload_redacted=…,
                          result='success')
   ON CONFLICT (event_source, dedup_key) DO NOTHING
   → if 0 rows inserted: replay; return 200 immediately            (CP-II / FR-005)
6. Dispatch by event_type:
     created  → services.cards.handle_firing(incident)
     updated  → services.cards.handle_update(incident)   # severity / summary changes
     closed   → services.cards.handle_resolved(incident)
7. Return 200 within 2 s (FR-001 / SC-001)
```

## Responses

| Status | Reason | Body |
|---|---|---|
| 200 | success (incl. dedup-replay short-circuit) | `{"ok": true}` |
| 401 | signature missing / mismatch / stale timestamp | `{"error": "signature"}` |
| 422 | body fails Pydantic validation | `{"error": "schema", "detail": …}` |
| 500 | unexpected (logged + reported to meta-channel; CP-VI) | `{"error": "internal"}` |

## Test obligations

- `tests/integration/signature/test_fd_*` — happy / tampered / missing / stale.
- `tests/integration/idempotency/test_fd_replay_100x.py` — same body × 100 → exactly
  one alerts row, one `lark.post_card` MockTransport call.
- `tests/integration/flows/test_firing_to_resolved.py` — created → closed produces
  the in-place card update (same `message_id`).
