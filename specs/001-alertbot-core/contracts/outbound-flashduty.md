# Outbound Contract — FlashDuty Schedule API (READ-ONLY)

**Counterparty**: FlashDuty schedule endpoints.
**Owner module**: `app/clients/flashduty.py`.
**Auth**: API token from Vault.
**Constitution gates**: I (this is the SOLE permitted upstream pull, with TTL ≤ 5 min).
**Spec FRs**: FR-012 / FR-013.

## Permitted operation

### `GET /api/v1/schedules?service=<service>&now=true` — read current on-call

(Endpoint shape canonical via [research.md §2](../research.md#2-flashduty-schedule-api)
and captured in `tests/fixtures/flashduty/schedule_*.json`.)

Returns the engineer currently on-call for `<service>` per FlashDuty's schedule
configuration. Result is cached in-process for **5 minutes** (FR-013 / Constitution
principle I exception). The cache key is `<service>`; cache value is the resolved
email (or `None` if no schedule).

```python
async def read_schedule(service: str) -> str | None:
    cached = _cache.get(service)
    if cached and cached.fresh_within(timedelta(minutes=5)):
        return cached.email
    # …else fetch + populate cache
```

## FORBIDDEN operations

The following endpoints exist in FlashDuty's API but **MUST NOT** be called by
AlertBot. This is enforced by code review and by a test that imports the
`clients.flashduty` module and asserts only `read_schedule` is exposed:

- `POST /api/v1/incidents/{id}/ack` — incident acknowledgement.
- `POST /api/v1/incidents/{id}/close` — incident close.
- `POST /api/v1/incidents/{id}/snooze` — incident snooze.
- Any endpoint that mutates state on FlashDuty's side.

This is FR-024, decided 2026-05-07: **AlertBot makes ZERO state-mutating calls to
FlashDuty.** Alertmanager is the sole silence source-of-truth. FlashDuty's incident
view may temporarily show an open incident while AM is silenced; this is documented
operational drift, not a bug.

## HTTP rules

- Timeout 5 s, retry max 3 (same as Constitution security default).
- Failure → fall through to the next tier in the D-plan (`static_service_map` →
  fallback role); meta-channel report on failure (CP-VI).

## Test obligations

- `tests/unit/clients/test_flashduty_readonly.py` — assert the public surface of
  `app/clients/flashduty.py` is exactly `read_schedule` and `parse_webhook` (the
  inbound-side helper); any other public callable fails the test. Regression guard
  for FR-024.
- `tests/integration/flows/test_oncall_cache_5min.py` — two consecutive `read_schedule`
  calls within 5 min produce exactly one MockTransport HTTP hit.
