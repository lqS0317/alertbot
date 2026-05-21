# Phase 0 Research — AlertBot Core

**Date**: 2026-05-07 · **Spec**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md)

This document resolves the technical research items needed before Phase 1 design.
Format: **Decision → Rationale → Alternatives considered**.

The spec entered Phase 0 with **zero `[NEEDS CLARIFICATION]` markers** (all three
were resolved on 2026-05-07 — see spec FR-022 / FR-023 / FR-024). What remains here
are technical-detail items flagged as "implementation research" in the spec
Assumptions section, plus library-choice items required by the plan.

---

## 1. FlashDuty webhook signature scheme

**Decision**: implement HMAC-SHA256 with the signing secret as the key, with the
canonical signing string `<unix-timestamp>.<raw-body>`. Verify
`X-FD-Signature: sha256=<hex>` header with constant-time comparison
(`hmac.compare_digest`); reject any timestamp older than 5 minutes (replay window).

**Rationale**:
- This is the FlashDuty (FlashCat) production scheme as of the latest documentation
  cycle and is consistent with the captured webhook fixtures in
  `tests/fixtures/flashduty/`.
- HMAC-SHA256 with a timestamped canonical string defeats both replay and
  body-length-extension attacks.
- Constant-time compare is required to defeat timing oracles on the secret.

**Alternatives considered**:
- *Plain shared-secret in a header*: rejected — vulnerable to log leakage and to
  TLS-termination introspection.
- *JWT in `Authorization`*: rejected — heavier, requires asymmetric keys we don't
  need; FlashDuty does not offer this option.

**Test obligations**: covered by `tests/integration/signature/test_fd_*.py` (T2): a
captured-real-payload happy case + tampered-body + missing-header + stale-timestamp.

---

## 2. FlashDuty schedule API

**Decision**: use the documented `GET /api/v1/schedules` (READ-ONLY) endpoint to
resolve the current on-call for a given service; cache the result in-process for
5 minutes per service. No other FlashDuty endpoint is called from
`app/clients/flashduty.py`.

**Rationale**:
- Constitution principle I forbids polling; FR-013 explicitly carves out a 5-minute
  TTL for schedule reads as the **single permitted exception**.
- A per-service cache (rather than a single global one) avoids re-reading the schedule
  for every alert, while keeping the staleness within the team's tolerance.
- 5 minutes matches the Constitution wording exactly; we MUST NOT extend this.

**Alternatives considered**:
- *No cache, read on every alert*: rejected — FlashDuty schedule changes are rare
  (hours-to-days scale) but alerts can come at burst rates of dozens per minute;
  caching is a correctness-equivalent optimisation.
- *Longer cache (≥ 1 h)*: rejected — Constitution caps at 5 minutes and that is the
  longest staleness the team is comfortable with for an on-call routing decision.

---

## 3. Lark form modal API

**Decision**: use Lark's interactive-card form modal, opened via the documented
"open form" call from a button-click handler. The form has a single field
("duration"); on submit, Lark dispatches the same `card.action.trigger` event back
to `/webhook/lark` with the form values in `event.action.value` (Shape 2 in
[`contracts/inbound-lark.md`](./contracts/inbound-lark.md)).

**Rationale**:
- The spec Assumptions explicitly select "Lark form modal" over secondary card or
  external web page (Brainstorming O2).
- The modal is part of the standard Lark interactive-card kit; it does not require
  additional Lark application permissions beyond `im:message` and the event-callback
  scope we already need.
- The submission lands on the same webhook route, so the dedup gate
  (`audit_log.dedup_key = event_id`) covers it identically.

**Alternatives considered**:
- *Secondary card with prefilled-time pickers*: rejected — heavier UX than the
  fixed buttons it'd parallel; defeats the purpose of "Custom" being the catch-all.
- *External web page*: rejected — adds a frontend, an auth dance, and a public
  surface unrelated to the bot's purpose.

---

## 4. Configuration hot-reload library

**Decision**: use **`watchdog`** (the Python file-system events library) to observe
the YAML config file's mount path; on change, re-load and re-validate via Pydantic;
swap the global config snapshot atomically using a `threading.RLock`-protected
module-level singleton.

**Rationale**:
- `watchdog` is platform-portable (works on Linux's `inotify`, macOS's `FSEvents`,
  and tolerates the symlink-rotation that K8s uses for ConfigMap projection).
- Atomic swap of an immutable Pydantic snapshot avoids torn reads (callers see
  either the old config in full or the new config in full, never a half-mutated
  one).
- Validation failure on reload keeps the previous snapshot and reports to the
  meta-channel — Constitution principle V + VI satisfied without a service crash.

**Alternatives considered**:
- *SIGHUP handler*: rejected — requires ops to remember to send a signal, which
  defeats "configuration change does not require a release/deploy step".
- *Polling the file mtime*: rejected — explicitly violates Constitution principle I.
- *Loading on every read*: rejected — unnecessary I/O on every alert.

**Test obligations**: covered by T6 — two integration tests, one for the hot-reload
happy path (write → observe → next call sees new value) and one for the validation
failure path (bad YAML → snapshot unchanged + meta-channel call).

---

## 5. Idempotency pattern (claim-check via audit_log)

**Decision**: Every inbound webhook handler's first DB action is an
`INSERT INTO audit_log (event_source, dedup_key, …)` with
`UNIQUE (event_source, dedup_key)`. On `IntegrityError` from a duplicate key, the
handler returns HTTP 200 immediately without invoking any business logic. This
pattern is sometimes called the "claim-check" pattern.

**Rationale**:
- Combines two Constitution requirements (II Idempotent + IV Audit) in one DB
  write, reducing transaction count.
- DB-level UNIQUE is a stronger guarantee than application-level dedup (FR-005
  explicitly mandates DB-level). It survives application-code bugs, race conditions
  between concurrent webhook deliveries to multiple replicas (although v1 runs as a
  single Pod, this future-proofs horizontal scale-out).
- The audit row records the duplicate attempt anyway: subsequent DB reads can
  observe the duplicate via the `audit_log` row, useful for debugging and SLA
  reporting.

**Dedup-key construction**:
- FlashDuty: `<incident_fingerprint>:<event_type>` (so `incident.created`,
  `incident.updated`, `incident.closed` for the same incident dedup independently).
  Note: this means a *second* `incident.updated` for the same incident WILL be
  re-processed; this is desired (severity changes etc. should be reflected). If
  per-update granularity is needed, FD's webhook payload includes a unique
  `event_id` we can append: `<fingerprint>:<event_type>:<event_id>`.
- Lark: `<header.event_id>` — Lark guarantees this is unique per delivery.

**Alternatives considered**:
- *Separate `webhook_events` table*: rejected — strictly equivalent guarantee at
  the cost of a table; the user's spec called for three tables and folding into
  `audit_log` is the cleanest fit. See plan §2.4.
- *In-memory dedup (LRU)*: rejected — does not survive process restart, fails
  Constitution principle II's "DB-level" requirement.
- *Redis SET-EX*: rejected — Constitution forbids Redis until a measured
  bottleneck appears.

---

## 6. Async DB engine choice

**Decision**: SQLAlchemy 2.0 async with **`asyncpg`** for PostgreSQL and
**`aiosqlite`** for SQLite. AsyncSession is constructed via
`async_sessionmaker(engine, expire_on_commit=False)`.

**Rationale**:
- `asyncpg` is the canonical high-performance async PostgreSQL driver. It supports
  PostgreSQL's `JSONB`, `INTERVAL`, and `gen_random_uuid()` natively.
- `aiosqlite` lets us run all integration tests against an in-memory SQLite DB,
  which keeps the test suite fast (no external service required).
- `expire_on_commit=False` is required for async usage because the default behaviour
  forces a refresh after commit, which would issue a synchronous query in an async
  context.

**Alternatives considered**:
- *psycopg3 async*: viable but currently lower throughput than `asyncpg` for our
  read-heavy patterns; revisit if asyncpg's API stability becomes an issue.
- *Synchronous SQLAlchemy with `run_in_executor`*: rejected — adds a thread-pool
  layer that fights against FastAPI's async event loop and complicates trace_id
  propagation (we'd need to thread the ContextVar manually).

---

## 7. Layer-direction enforcement

**Decision**: use **`import-linter`** with a `"layered"` contract in
`.importlinter`:

```ini
[importlinter]
root_package = app

[importlinter:contract:layered-arch]
name = AlertBot 4-layer dependency direction
type = layered
layers =
    app.webhooks
    app.services
    app.clients
    app.models
ignore_imports =
    app.webhooks ** -> app.config
    app.webhooks ** -> app.observability
    app.services ** -> app.config
    app.services ** -> app.observability
    app.clients ** -> app.config
    app.clients ** -> app.observability
```

**Rationale**:
- Mechanical enforcement is more reliable than code-review rigour, especially as
  the team grows. `import-linter` runs in CI and fails the build on a violation.
- The two cross-cutting modules (`config`, `observability`) are explicitly allowed
  to be imported from any layer — this is the standard "ambient" exception.

**Alternatives considered**:
- *Code review only*: rejected — humans miss things; CI does not.
- *Hand-written `tests/test_layering.py` using `ast`*: rejected — re-implements
  what `import-linter` already does well.

---

## 8. Lark application form (self-built vs. custom-webhook bot)

**Decision**: use the **enterprise self-built Lark application** form, NOT the
"custom webhook bot" form.

**Rationale** (re-stated from spec Assumptions for completeness):
- Self-built apps can hold OAuth credentials, call user-info APIs (needed for
  `lookup_user_email`), and use the full `im:message` PATCH/PUT surface.
- Custom-webhook bots cannot patch messages they posted, which would defeat the
  "same `message_id` for state transitions" requirement (FR-010 / SC-010).

**Alternatives considered**: none. The self-built form is the only one that
satisfies FR-010.

---

## 9. Outbound retry / backoff library

**Decision**: use a small in-house retry helper built on top of `httpx`'s
`AsyncClient.send` rather than pulling in a dedicated library (e.g. `tenacity`).
Behaviour: max 3 attempts, exponential backoff `1 s → 2 s → 4 s`, jitter ±25 %,
retry only on `TimeoutException` / `ConnectError` / 5xx status codes.

**Rationale**:
- Adding `tenacity` adds a dependency for ~30 lines of code we can write
  predictably.
- Constitution security policy specifies the exact policy ("default 5 s + 3
  retries exponential"), and we want it visible in the codebase, not buried in
  a library configuration.

**Alternatives considered**:
- *`tenacity`*: rejected — extra dependency surface for a small benefit.
- *`stamina`*: rejected — newer; not yet on the team's known-stable list.

---

## 10. structlog configuration

**Decision**: configure `structlog` to emit JSON lines to stdout, with these
processors in order: `add_log_level` → `TimeStamper(fmt="iso", utc=True)` →
`merge_contextvars` (picks up `trace_id`) → `JSONRenderer()`.

**Rationale**:
- JSON-on-stdout is the K8s-native logging contract.
- `merge_contextvars` is the standard mechanism to thread `trace_id` from a
  FastAPI middleware ContextVar into every log line without adding a logger
  parameter to every function.
- ISO-8601 UTC timestamps are unambiguous and sortable.

**Alternatives considered**:
- *Python `logging` with custom Formatter*: rejected — heavier setup, harder to
  add structured fields.
- *`structlog` with a console renderer in dev*: deferred — switching renderer by
  env is a follow-up polish (T24's documentation mentions this as a future
  enhancement).

---

## Summary table

| Topic | Decision | Library / pattern |
|---|---|---|
| FD signature | HMAC-SHA256 over `<ts>.<body>`; 5-min replay window | stdlib `hmac` |
| FD schedule | READ-ONLY GET with 5-min in-process cache | stdlib + custom |
| Lark form modal | standard interactive-card form | Lark Open API |
| Config hot-reload | watchdog → revalidate → atomic snapshot swap | `watchdog` |
| Idempotency | claim-check via `audit_log` UNIQUE | SQLAlchemy + DB |
| Async DB | SQLAlchemy 2.0 async; asyncpg + aiosqlite | as named |
| Layer enforcement | import-linter "layered" contract | `import-linter` |
| Lark app form | enterprise self-built | n/a |
| Retry policy | in-house helper, 3× exp backoff, 5xx + timeout only | `httpx` |
| Logging | structlog JSON to stdout + ContextVar trace_id | `structlog` |

All Phase 0 items are resolved; Phase 1 (data-model + contracts + quickstart) is
unblocked.
