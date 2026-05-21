# Inbound Contract — Lark Webhook (`POST /webhook/lark`)

**Counterparty**: Lark Open Platform (https://open.larksuite.com / https://open.feishu.cn).
**Owner module**: `app/webhooks/lark.py`.
**Constitution gates**: VIII (`url_verification` first), VII (verify), II (idempotent), IV (audit).
**Spec FRs**: FR-003 / FR-004 / FR-005 / FR-015 / FR-019.

This route handles **three** distinct request shapes on the same path. The
`url_verification` shape MUST be matched and answered before any signature step (CP-VIII).

## Request — Shape 1: URL verification handshake

| Item | Value |
|---|---|
| Method | `POST` |
| Path | `/webhook/lark` |
| Body | `{"type": "url_verification", "challenge": "<value>", "token": "<verification-token>"}` |
| Signature | NOT present on the handshake |

### Required response (within 5 s; FR-004 / SC-001)

```json
{"challenge": "<value>"}
```

This MUST be the FIRST branch in the route handler, BEFORE signature verification.

## Request — Shape 2: Card action trigger (silence button click)

| Item | Value |
|---|---|
| Method | `POST` |
| Path | `/webhook/lark` |
| Headers | `X-Lark-Request-Timestamp`, `X-Lark-Request-Nonce`, `X-Lark-Signature` |
| Encryption | optional (Encrypt Key + AES); decrypted before parse |
| Body | JSON; `header.event_type = "card.action.trigger"` |

### Body shape (post-decryption)

```json
{
  "schema": "2.0",
  "header": {
    "event_id": "lark-evt-9f1b22…",
    "event_type": "card.action.trigger",
    "create_time": "1746601250000",
    "tenant_key": "…",
    "app_id": "cli_abc"
  },
  "event": {
    "operator": {
      "open_id": "ou_alice…",
      "user_id": "u_alice…",
      "tenant_key": "…"
    },
    "token": "card-action-token-xyz",
    "action": {
      "tag": "button",
      "value": {
        "kind": "silence",
        "alert_fingerprint": "alertname=HighCPU,instance=web-01",
        "duration": "30min"
      }
    }
  }
}
```

`value.kind` ∈ `{silence, custom_open}`. The `custom_open` variant carries
`{"kind": "custom_open"}` and triggers the form modal (US4); the form-submit callback
arrives back on this same route with `value.kind = "silence"` and `duration` parsed
from the form input.

## Request — Shape 3: General event_callback (reserved)

Not used in v1. The route returns 200 with a no-op for unrecognised `event_type` so
Lark does not retry indefinitely; an audit row is still recorded with
`operation = "webhook.lark.unhandled"`.

## Processing pipeline

```
1. Parse JSON body without validating signature (we need to detect Shape 1)
2. If body.type == "url_verification":                              (CP-VIII / FR-004)
       return {"challenge": body.challenge}                           # ≤ 5 s
3. If body is encrypted: AES-decrypt with Encrypt Key
4. Verify signature: HMAC(Verification-Token + timestamp + nonce + body) == header
   → 401 on mismatch                                                 (CP-VII / FR-003)
5. Reject timestamps older than 5 min (replay window)                → 401
6. INSERT INTO audit_log (event_source='lark',
                          dedup_key=header.event_id,
                          operation='webhook.lark.received',
                          payload_redacted=…)
   ON CONFLICT (event_source, dedup_key) DO NOTHING
   → if 0 rows inserted: replay; return 200 immediately              (CP-II / FR-005)
7. Dispatch by event.action.value.kind:
     silence       → services.cards.handle_silence_click(...)
     custom_open   → clients.lark.open_form_modal(...)
8. Return 200 within 2 s (FR-001 analogue / SC-002)
```

## Authorization

**No authorisation check** is performed on the click. Any group member can click any
silence button (FR-023, decided 2026-05-07). Accountability is enforced solely by
recording the real operator email in `silences.created_by` and in the audit row.

## Responses

| Status | Reason | Body |
|---|---|---|
| 200 | success (incl. handshake; incl. dedup-replay short-circuit) | `{"ok": true}` or `{"challenge": …}` |
| 401 | signature missing / mismatch / stale timestamp | `{"error": "signature"}` |
| 422 | body fails Pydantic validation post-decryption | `{"error": "schema"}` |
| 500 | unexpected | `{"error": "internal"}` |

## Test obligations

- `tests/integration/signature/test_lark_url_verification.py` — handshake answered
  before any signature step (CP-VIII regression test).
- `tests/integration/signature/test_lark_signature.py` — happy / tampered / missing /
  stale / encrypted-and-decrypted.
- `tests/integration/idempotency/test_lark_replay_100x.py` — same `event_id` × 100 →
  exactly one silence row, one `alertmanager.create_silence` MockTransport call.
- `tests/integration/flows/test_silence_click_to_silenced_card.py` — full SILENCED
  flow with MockTransport on the AM and Lark patch sides.
