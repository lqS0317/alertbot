# Final Acceptance Review — AlertBot Core

This document maps the implemented feature to the spec, success criteria, and
AlertBot Constitution v1.0.0.

## User Stories

- US1: FlashDuty alerts appear as styled Lark cards and resolve in place.
- US2: Cards @-mention the resolved on-call target through the D-plan chain.
- US3: Fixed-duration silence buttons create Alertmanager silences and patch the same
  card to `silenced`.
- US4: Custom duration opens a Lark form modal; valid values reuse the silence flow;
  invalid or >24h values are rejected before Alertmanager calls.

## Success Criteria Trace

- SC-001: Covered by end-to-end firing card tests and `/metrics` route durations.
- SC-002: Covered by US3 silence flow tests and Alertmanager client tests.
- SC-003: Covered by FlashDuty and Lark replay/idempotency tests.
- SC-004: Covered by real-email and `lark:<user_id>` fallback tests.
- SC-005: Requires staging / production observation after rollout.
- SC-006: Audit rows exist for inbound webhook and Lark event claim-check paths.
- SC-007: Meta-channel report paths are covered for AM failure and missing email.
- SC-008: Covered by route-level >24h rejection and DB CHECK tests.
- SC-009: Covered by hot-reload E2E guard and documented staging check.
- SC-010: Covered by resolved-card in-place patch tests.

## Constitution Trace

- I. Webhook-First, Polling-Last: inbound flow uses webhooks; schedule read is TTL cached.
- II. Idempotent & Replay-Safe: database uniqueness and replay tests cover FD and Lark.
- III. Test-First Development: each phase was implemented from failing tests first.
- IV. Audit Everything: inbound dedup uses `audit_log` claim-check; silence flow writes rows.
- V. Config-Driven, Not Hardcoded: YAML + Pydantic config owns policy values.
- VI. Fail Fast & Visible: failures report to meta-channel with trace context.
- VII. Verify Every Webhook: FD and Lark signature tests cover tampered/missing/stale cases.
- VIII. Lark URL Verification First: url verification route is tested before signature logic.

## Manual Sign-off Still Required

- T047: staging firing → resolved smoke test.
- T099: 24-hour production canary with SC-001/002/003/004 metrics reviewed.
