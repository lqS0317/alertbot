# Contracts — AlertBot Core

This directory captures the wire-level contracts AlertBot honours at every boundary.
Two **inbound** routes (HTTP servers we expose) and three **outbound** clients (HTTP
clients we own).

| File | Direction | Counterparty | Purpose |
|---|---|---|---|
| [`inbound-flashduty.md`](./inbound-flashduty.md) | Inbound | FlashDuty | `POST /webhook/fd` — incident lifecycle webhooks |
| [`inbound-lark.md`](./inbound-lark.md) | Inbound | Lark Open Platform | `POST /webhook/lark` — `url_verification` + `card.action.trigger` + `event_callback` |
| [`outbound-lark.md`](./outbound-lark.md) | Outbound | Lark Open Platform | post / patch interactive cards; user lookup; form modal |
| [`outbound-flashduty.md`](./outbound-flashduty.md) | Outbound | FlashDuty | **READ-ONLY** schedule API |
| [`outbound-alertmanager.md`](./outbound-alertmanager.md) | Outbound | Alertmanager | `POST /api/v2/silences` |

Live captured samples live in `tests/fixtures/flashduty/` and `tests/fixtures/lark/`;
those samples are the truth source for payload shapes consumed by integration tests
(via `httpx.MockTransport`).
