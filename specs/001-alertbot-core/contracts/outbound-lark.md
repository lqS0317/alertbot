# Outbound Contract — Lark Open Platform

**Counterparty**: Lark Open Platform.
**Owner module**: `app/clients/lark.py`.
**Auth**: tenant access token, refreshed via `app_id` + `app_secret` from Vault.
**Constitution gates**: I (no polling), VI (fail-fast), security req. "outbound timeout + retry".
**Spec FRs**: FR-006..010 / FR-014 / FR-019 / FR-020.

## Operations used

### `POST /open-apis/im/v1/messages?receive_id_type=chat_id` — post interactive card

Posts a new message of type `interactive` to the configured group chat. Returns the
`message_id` which AlertBot persists on the `alerts` row (FR-009) for later in-place
patches.

```json
{
  "receive_id": "oc_alertbot_group",
  "msg_type":   "interactive",
  "content":    "<JSON-stringified card payload>"
}
```

The card payload itself is built by `services.cards.render(state, alert, ...)` and
varies by state (firing / silenced / resolved). Card-payload shapes are validated by
unit tests against captured Lark fixtures.

### `PATCH /open-apis/im/v1/messages/{message_id}` — in-place card update

The single permitted update path. Used for all state transitions
(`firing → silenced`, `firing → resolved`, `silenced → resolved`). Body is the new
card payload; same `message_id` (FR-010 / SC-010).

If Lark returns `404 message not found` (the `message_id` was lost on Lark's side),
fall through to the **"Original card lost" fallback path** (FR-011): post a new card
prefixed `[Original card lost]` and report the inconsistency to the meta-channel.

### `GET /open-apis/contact/v3/users/{user_id}?user_id_type=open_id` — user lookup

Used by `lookup_user_email(user_id) → str | None`. Result is cached per-process
(no TTL — `user_id ↔ email` mapping is stable in Lark).

### `POST /open-apis/im/v1/cards/forms` — open form modal (US4)

Triggered when an engineer taps the `[Custom]` button. Opens a modal with a single
duration-input field. The submission arrives back via the `card.action.trigger`
inbound route (see [`inbound-lark.md`](./inbound-lark.md) — Shape 2).

Exact endpoint name and body schema confirmed in
[research.md §3](../research.md#3-lark-form-modal-api).

## HTTP rules (FR-027 / Constitution security)

- Timeout: 5 s.
- Retry: exponential backoff, max 3 attempts; retry on timeout / connection error
  / 5xx.
- Token refresh on 401: re-fetch tenant access token once and retry the original
  request once. Persistent 401 → meta-channel critical.

## Failure modes

| Mode | Code | Action |
|---|---|---|
| post_card timeout / 5xx | retry → terminal | abort the FIRING flow; meta-channel report; **do NOT** attempt a stale post on next webhook (the audit-log dedup row prevents re-processing) |
| patch_card 404 | terminal | invoke "Original card lost" fallback (FR-011); post a new card; meta-channel report |
| patch_card timeout / 5xx | retry → terminal | meta-channel report; leave the card in its previous state (no fake `silenced`) (FR-020) |
| lookup_user_email 404 | terminal | use `lark:<user_id>` fallback (FR-018); meta-channel report |

## Test obligations

- `tests/unit/services/test_card_renderer.py` — card payloads for each state match
  captured fixtures, including @-mention rendering for each oncall-resolution tier.
- `tests/integration/flows/test_post_card_persists_message_id.py` — MockTransport
  returns a `message_id`; `alerts.lark_message_id` is populated in the same
  transaction as the `audit_log` row.
- `tests/integration/flows/test_patch_card_uses_same_message_id.py` — second event
  PATCHes the same id (regression for the in-place rule, SC-010).
- `tests/integration/flows/test_card_lost_fallback.py` — MockTransport returns 404
  on PATCH → fallback path posts new "Original card lost" card + meta-channel.
