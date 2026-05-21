# Outbound Contract — Alertmanager Silence API

**Counterparty**: Alertmanager `/api/v2/silences`.
**Owner module**: `app/clients/alertmanager.py`.
**Auth**: bearer-token from a least-privilege service account (silences-only scope).
**Constitution gates**: VI (fail-fast), security req. "outbound timeout + retry".
**Spec FRs**: FR-015 / FR-017 / FR-020 / FR-027.

## Operations used

### `POST /api/v2/silences` — create

#### Request body

```json
{
  "matchers": [
    {"name": "alertname", "value": "HighCPU", "isRegex": false, "isEqual": true},
    {"name": "instance",  "value": "web-01",  "isRegex": false, "isEqual": true}
  ],
  "startsAt": "2026-05-07T08:05:00Z",
  "endsAt":   "2026-05-07T08:35:00Z",
  "createdBy": "alice@company.com",
  "comment":   "Silenced from Lark by Alice (incident=alertname=HighCPU,instance=web-01)"
}
```

**Matcher translation rule** (`app/clients/alertmanager.py::matchers_from_labels`):
- All `alert.labels.*` are translated to exact-match matchers (`isRegex=false`,
  `isEqual=true`).
- Two label keys are excluded from matchers (they are AlertBot-internal): `lark_user`,
  `flashduty_team`. Configurable via `oncall.matcher_exclude_keys` if more emerge.

**`createdBy` rules** (FR-015 / FR-018, SC-004):
- Lookup operator's email via `clients.lark.lookup_user_email(user_id)`.
- Email present → use it verbatim.
- Email absent → use `lark:<user_id>` and report missing-email to the meta-channel.
- The bot's own identity is **never** used here.

**Hard cap** (FR-017, SC-008):
- `endsAt - startsAt` ≤ 24 hours, enforced
  - at the route layer (T15) — reject before any HTTP call,
  - at the SQL CHECK constraint (T13 / data-model.md) — DB-level last line of defence.

#### HTTP rules (FR-027 / Constitution security)

- `httpx.AsyncClient(timeout=5.0)`
- Retry policy: exponential backoff, max 3 attempts (1 s → 2 s → 4 s).
- Retry only on: `httpx.TimeoutException`, `httpx.ConnectError`, 5xx status codes.
- Do NOT retry on: 4xx (these are caller bugs).

#### Responses

| Status | Meaning | Our action |
|---|---|---|
| 200 / 201 | created; body returns `{"silenceID": "<uuid>"}` | persist `Silence`; UPDATE `alerts.state = silenced`; PATCH Lark card to silenced |
| 400 | bad matchers / dates | meta-channel report; surface inline failure on card; **no retry** |
| 401 / 403 | service-account credentials wrong | meta-channel critical; surface inline failure |
| 5xx | AM unhealthy | retry up to 3; if still failing, meta-channel + inline failure on card (T16) |
| (timeout) | network or AM hang | retry up to 3; same fallback |

### `GET /api/v2/silences` — list (read-only, used by reaper)

Used by the periodic state reconciler (Phase 4 polish, not in T1–T25 directly) to
mark `silences.state = expired` for rows whose `endsAt` has passed. Read-only.

### `DELETE /api/v2/silence/{id}` — **NOT USED**

v1 does not cancel silences via API (FR-022, dropped). This endpoint is intentionally
absent from `app/clients/alertmanager.py`.

## Test obligations

- `tests/unit/clients/test_matcher_translation.py` — labels → matchers correctness.
- `tests/integration/flows/test_create_silence_happy.py` — MockTransport returns 201,
  full row persisted with correct `createdBy`.
- `tests/integration/flows/test_create_silence_failure.py` — MockTransport returns
  502 → 3 retries observed → terminal failure → inline card notice + meta-channel.
- `tests/integration/flows/test_create_silence_timeout.py` — MockTransport delays
  > 5 s → timeout exception → same retry/failure path.
- `tests/unit/models/test_silences_24h_check.py` — direct SQL INSERT with > 24 h
  raises CHECK violation.
