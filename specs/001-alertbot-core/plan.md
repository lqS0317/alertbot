# Implementation Plan: AlertBot Core

**Branch**: `001-alertbot-core` | **Date**: 2026-05-07 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification at `specs/001-alertbot-core/spec.md`
**Constitution**: AlertBot Constitution v1.0.0 (`.specify/memory/constitution.md`)

> Companion artifacts (Phase 0 / Phase 1 outputs of this plan):
> - [research.md](./research.md) — Phase 0 research outcomes
> - [data-model.md](./data-model.md) — full SQLAlchemy 2.0 model definitions
> - [contracts/](./contracts/) — inbound webhook + outbound API contracts
> - [quickstart.md](./quickstart.md) — local SQLite run + smoke test

---

## Summary

AlertBot is a single-process FastAPI service that receives FlashDuty incident webhooks,
posts severity-coloured interactive cards to a designated Lark group with the on-call
engineer @-mentioned, exposes six silence buttons (`5min` / `30min` / `1h` / `4h` /
`24h` / `Custom`), and creates Alertmanager silences with the **real operator's email**
in the `createdBy` field on click. Card state transitions (`firing → silenced → resolved`)
are applied as in-place patches to a single `message_id`. Idempotency, signature
verification, and audit are enforced at every boundary.

This plan covers four delivery phases mapped to the four user stories in the spec, with
**25 tasks (T1–T25)** sized at 2–5 minutes each, executed test-first per Constitution
principle III.

---

## Technical Context

| Field | Value |
|---|---|
| Language / Version | Python 3.11+ (Constitution-fixed) |
| HTTP framework | FastAPI |
| HTTP client | HTTPX (async, with `MockTransport` for tests) |
| ORM | SQLAlchemy 2.0 (async) |
| Data validation | Pydantic v2 |
| Storage (production / staging) | PostgreSQL |
| Storage (local + tests) | SQLite (file or `:memory:`) |
| Test framework | pytest + pytest-asyncio |
| Type checking | `mypy --strict` |
| Linting / formatting | `black` + `ruff` |
| Container | Docker (multi-stage) |
| Deployment | Kubernetes via Helm chart |
| Secrets | HashiCorp Vault or Sealed Secret |
| Project type | Single web service (one Python project, one Pod) |
| Performance targets | webhook handler p95 ≤ 2 s; Lark `url_verification` ≤ 5 s |
| Scale (v1) | one team / one Lark group / one Alertmanager; ≤ 1 000 incidents/day expected |
| Explicitly forbidden | LangChain · LangGraph · vector DBs · MCP · Redis (unless measured dedup bottleneck) · message queues · Celery |

There are no `NEEDS CLARIFICATION` items remaining. All three open questions in the
Phase-0-equivalent spec round were resolved on 2026-05-07 (see spec FR-022 / FR-023 /
FR-024) and bind this plan.

---

## 1. Architecture Overview

### 1.1 Four-layer architecture (mandatory dependency direction)

```
                ┌────────────────────────────────────────────────────────┐
                │  Layer 1 · webhooks/      (FastAPI routers)            │
                │  - signature verification                              │
                │  - Lark url_verification handshake (always first)      │
                │  - dedup gate (audit-log INSERT)                       │
                │  - dispatch to services                                │
                └──────────────────────────┬─────────────────────────────┘
                                           │  depends on  ↓
                ┌──────────────────────────▼─────────────────────────────┐
                │  Layer 2 · services/      (business rules)             │
                │  - oncall.resolve()  (D-plan, 5-min cache)             │
                │  - cards.render() / cards.transition()                 │
                │  - audit.record()    (write-with-dedup)                │
                └──────────────────────────┬─────────────────────────────┘
                                           │  depends on  ↓
                ┌──────────────────────────▼─────────────────────────────┐
                │  Layer 3 · clients/       (external IO, async HTTPX)   │
                │  - lark.post_card / lark.patch_card / lark.lookup_user │
                │  - lark.open_form_modal                                │
                │  - flashduty.read_schedule          (READ-ONLY)        │
                │  - alertmanager.create_silence                         │
                └──────────────────────────┬─────────────────────────────┘
                                           │  depends on  ↓
                ┌──────────────────────────▼─────────────────────────────┐
                │  Layer 4 · models.py       (SQLAlchemy + AsyncSession) │
                │  - Alert · Silence · AuditLog                          │
                └────────────────────────────────────────────────────────┘

      ┌────────────────────── horizontal cross-cutting ───────────────────────┐
      │  app/config.py         YAML loader + Pydantic v2 schema + hot-reload  │
      │  app/observability.py  structlog + trace_id contextvar + meta-reporter│
      └───────────────────────────────────────────────────────────────────────┘
```

**Hard rule (enforced via `import-linter` in CI)**:
- A module in layer N MUST NOT import from any module in layers `< N` (i.e. higher
  layers).
- `clients/` MUST NOT import from `services/` or `webhooks/`.
- `models.py` MUST NOT import from any of `clients/` / `services/` / `webhooks/`.
- `config.py` and `observability.py` may be imported from any layer (cross-cutting).

### 1.2 Three end-to-end request flows

```
A) FIRING:    FlashDuty incident.created
              → webhooks.flashduty (verify, dedup INSERT into audit_log)
              → services.oncall.resolve() ── reads ──> clients.flashduty.read_schedule
              → services.cards.render(firing)
              → clients.lark.post_card  ────────────> Lark message_id
              → models: INSERT alerts (incident_fingerprint UNIQUE, lark_message_id)
              → services.audit.record('webhook.fd.received', success)
              ⤷ return 200 within 2 s p95

B) SILENCED:  Lark card.action.trigger (button click)
              → webhooks.lark (url_verification short-circuit; verify; dedup INSERT)
              → clients.lark.lookup_user(user_id) ──> email
              → services.cards.silence_payload(...)
              → clients.alertmanager.create_silence(matchers, endsAt, createdBy=email)
              → models: INSERT silences (alertmanager_silence_id UNIQUE)
              → models: UPDATE alerts.state = 'silenced'
              → clients.lark.patch_card(message_id, silenced_state)
              → services.audit.record('alertmanager.silence.create', success)
              ⤷ return 200 within 2 s p95

C) RESOLVED:  FlashDuty incident.closed
              → webhooks.flashduty (verify, dedup INSERT)
              → models: SELECT alerts WHERE incident_fingerprint = ?
              → models: UPDATE alerts.state = 'resolved'
              → clients.lark.patch_card(message_id, resolved_state)
              → services.audit.record('webhook.fd.received[closed]', success)
              ⤷ return 200 within 2 s p95
```

### 1.3 Source code layout

```
app/
├── __init__.py
├── main.py                  # FastAPI app factory + lifespan (DB engine, hot-reload, shutdown)
├── config.py                # YAML loader + Pydantic v2 schema + watchdog hot-reload
├── observability.py         # structlog setup; trace_id ContextVar; meta-channel reporter
├── webhooks/
│   ├── __init__.py
│   ├── flashduty.py         # POST /webhook/fd
│   └── lark.py              # POST /webhook/lark   (url_verification + event_callback)
├── services/
│   ├── __init__.py
│   ├── oncall.py            # 4-tier D-plan resolver + 5-min schedule cache
│   ├── cards.py             # card payload builders + state machine
│   └── audit.py             # write-with-dedup gateway (claim-check pattern)
├── clients/
│   ├── __init__.py
│   ├── lark.py              # post_card, patch_card, lookup_user_email, open_form_modal
│   ├── flashduty.py         # read_schedule (READ-ONLY; no incident state writes)
│   └── alertmanager.py      # create_silence
└── models.py                # Alert · Silence · AuditLog · AsyncSession factory

config/
├── example.yaml             # documented sample with all keys
└── README.md                # how to override per env

tests/
├── conftest.py              # MockTransport fixtures, async DB fixtures, time control
├── unit/                    # mirrors app/  (oncall priority chain, card renderers, etc.)
│   ├── services/
│   ├── clients/
│   └── webhooks/
├── integration/             # mirrors app/  (full round-trip with MockTransport)
│   ├── flows/               # FIRING / SILENCED / RESOLVED end-to-end tests
│   ├── idempotency/         # SC-003 100x replay tests
│   └── signature/           # happy + tampered + missing + url_verification
└── fixtures/
    ├── flashduty/           # captured real webhook samples (incident.created/updated/closed)
    └── lark/                # captured url_verification + card.action.trigger samples

deploy/
├── Dockerfile               # multi-stage: builder (poetry install) → runtime (slim)
├── compose.yaml             # local: SQLite + tunnel
└── helm/
    └── alertbot/
        ├── Chart.yaml
        ├── values.yaml      # default values
        ├── values-staging.yaml
        ├── values-prod.yaml
        └── templates/
            ├── deployment.yaml
            ├── service.yaml
            ├── ingress.yaml      # public ingress with TLS
            ├── configmap.yaml    # YAML config (NOT secrets)
            ├── sealedsecret.yaml # Lark + FD + AM credentials
            └── serviceaccount.yaml

pyproject.toml               # poetry; deps + black/ruff/mypy/pytest config
.importlinter                # enforces 4-layer dependency direction in CI
.github/workflows/ci.yml     # lint + typecheck + test + coverage
```

**Structure decision**: Single Python project, single FastAPI process, single Pod. No
"backend/frontend" split (no UI). The `app/` package is the deliverable; `deploy/`
holds the container + Helm chart; `tests/` mirrors `app/`.

### 1.4 Cross-cutting

- **Configuration** (`app/config.py`): a single `AlertBotConfig` Pydantic model is the
  source of truth. YAML is loaded at startup and re-loaded on file-change events
  (watchdog). All callers obtain config via `get_config()` which returns the latest
  validated snapshot — never a stale module-level constant. Hot-reload covers FR-029
  / SC-009.
- **Observability** (`app/observability.py`): every inbound request is assigned a
  `trace_id` (via FastAPI middleware) which is stored in a `ContextVar` and threaded
  through all logs, audit rows, and meta-channel reports. The meta-channel reporter
  is a small async client that posts a message to a configured Lark "ops" group; it
  is invoked from every `except` site that re-raises and from every outbound-failure
  path (Constitution principle VI / FR-026 / FR-028).

---

## 2. Data Model

> Full SQLAlchemy 2.0 declarative definitions live in [data-model.md](./data-model.md).
> What follows is the schema summary that the plan commits to.

Three tables. All UNIQUE constraints below are **DB-level**, satisfying FR-005 and
Constitution principle II.

### 2.1 `alerts`

| Column | Type | Constraints / notes |
|---|---|---|
| `id` | BIGSERIAL | PK |
| `incident_fingerprint` | VARCHAR(255) | **UNIQUE NOT NULL** — dedup gate for `incident.created` (FR-005, SC-003) |
| `service` | VARCHAR(255) | NOT NULL — used by oncall resolver and silence matchers |
| `severity` | VARCHAR(32) | NOT NULL — drives card title-bar colour (FR-006) |
| `summary` | TEXT | NOT NULL — rendered on card body (FR-007) |
| `labels` | JSONB | NOT NULL — full FlashDuty incident labels; source of silence matchers |
| `lark_message_id` | VARCHAR(64) | NOT NULL — UPDATE target for in-place patches (FR-009/010) |
| `state` | ENUM(`firing`,`silenced`,`resolved`) | NOT NULL DEFAULT `firing` |
| `oncall_target` | VARCHAR(255) | resolved D-plan output: `lark_user@…` / role / `lark:<id>` |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT `now()` |
| `updated_at` | TIMESTAMPTZ | NOT NULL DEFAULT `now()` ON UPDATE |

Indexes: `(state, updated_at)` for operational queries; `(service)` for analytics.

### 2.2 `silences`

| Column | Type | Constraints / notes |
|---|---|---|
| `id` | BIGSERIAL | PK |
| `alertmanager_silence_id` | VARCHAR(64) | **UNIQUE NOT NULL** — Alertmanager-side ID; enforces "one click → one silence" |
| `lark_event_id` | VARCHAR(128) | **UNIQUE NOT NULL** — Lark `card.action.trigger` event_id; enforces idempotency on duplicate Lark callbacks (FR-005) |
| `alert_fingerprint` | VARCHAR(255) | NOT NULL — FK-style reference to `alerts.incident_fingerprint` |
| `matchers` | JSONB | NOT NULL — Alertmanager matcher list (translated from alert labels) |
| `created_by` | VARCHAR(255) | NOT NULL — real operator email or `lark:<user_id>` fallback (FR-015 / FR-018, SC-004) |
| `actor_lark_user_id` | VARCHAR(64) | NOT NULL — Lark user_id of the clicker; co-stored even when email is present |
| `starts_at` | TIMESTAMPTZ | NOT NULL |
| `ends_at` | TIMESTAMPTZ | NOT NULL — **CHECK (ends_at - starts_at) <= INTERVAL '24 hours'** (FR-017 / SC-008) |
| `duration_choice` | VARCHAR(16) | NOT NULL — `5min` / `30min` / `1h` / `4h` / `24h` / `custom` |
| `state` | ENUM(`active`,`expired`,`cancelled`) | NOT NULL DEFAULT `active` |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT `now()` |

Indexes: `(alert_fingerprint)`, `(state, ends_at)`.

### 2.3 `audit_log`

This table doubles as the **dedup gate** for inbound webhooks (claim-check pattern)
and as the audit trail for FR-025.

| Column | Type | Constraints / notes |
|---|---|---|
| `id` | BIGSERIAL | PK |
| `trace_id` | VARCHAR(64) | NOT NULL — propagated from FastAPI middleware |
| `timestamp_utc` | TIMESTAMPTZ | NOT NULL DEFAULT `now()` |
| `event_source` | ENUM(`flashduty`,`lark`,`alertmanager`,`internal`) | NOT NULL |
| `dedup_key` | VARCHAR(255) | NULLable — `<fingerprint>:<event_type>` for FD, Lark `event_id` for Lark, NULL for outbound rows |
| `operation` | VARCHAR(64) | NOT NULL — e.g. `webhook.fd.received`, `lark.card.update`, `alertmanager.silence.create`, `card.action.silence.click` |
| `actor_lark_user_id` | VARCHAR(64) | NULLable — populated for button clicks |
| `actor_email_redacted` | VARCHAR(255) | NULLable — last-2 + domain only (e.g. `**@company.com`) |
| `payload_redacted` | JSONB | NOT NULL — secrets + tokens stripped via `app.observability.redact()` |
| `result` | ENUM(`success`,`failure`) | NOT NULL |
| `result_summary` | VARCHAR(512) | NULLable — HTTP status, error message |

**Constraint**: `UNIQUE (event_source, dedup_key) WHERE dedup_key IS NOT NULL`. This
is the single mechanism that satisfies "DB-level unique constraint enforces
deduplication" (FR-005). On duplicate inbound webhook, the `INSERT` raises
`IntegrityError` → handler short-circuits with HTTP 200, no business logic runs.

### 2.4 Why three tables (and not four)

The user-given spec requires Alert / Silence / Audit (three tables). A separate
`webhook_events` table was considered for dedup but rejected: folding the dedup key
into `audit_log` is strictly equivalent (both are write-once at the inbound boundary)
and saves a table without weakening the guarantee. The `audit_log` row IS the
"received" claim-check.

---

## 3. Configuration Schema (Pydantic v2)

YAML config is loaded, validated, and hot-reloaded by `app/config.py`. Schema:

```python
# Sketch — full code lives in app/config.py once T6 lands.
class AlertBotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lark: LarkConfig                 # app credentials (refs to secrets), group_id, meta_channel_id
    flashduty: FlashdutyConfig       # webhook signing secret ref, schedule API base URL
    alertmanager: AlertmanagerConfig # base URL, service-account ref, request timeout (default 5s)

    oncall: OncallConfig             # priority chain order, static service→user map, fallback role
    severity_colors: dict[str, str]  # e.g. {"critical": "red", "warning": "orange", "info": "blue"}
    silence_buttons: SilenceButtonsConfig
    timezone: str                    # IANA name, default "Asia/Shanghai"
    max_silence_hours: int = Field(default=24, le=24)  # FR-017 hard cap, cannot be raised

class OncallConfig(BaseModel):
    priority_chain: list[Literal["incident_label", "fd_schedule", "static_map", "fallback_role"]]
    static_service_map: dict[str, str]   # service_name -> lark email
    fallback_role: str                   # e.g. "@on-call"
    schedule_cache_ttl_seconds: int = Field(default=300, ge=0, le=300)  # ≤ 5 min (FR-013)

class SilenceButtonsConfig(BaseModel):
    fixed_durations: list[Literal["5min","30min","1h","4h","24h"]] = ["5min","30min","1h","4h","24h"]
    enable_custom: bool = True
```

Hot-reload is implemented via `watchdog` observing the config file mount path; on
change, the file is re-loaded, re-validated, and the global snapshot atomically
replaced (`get_config()` returns the new snapshot on next call). On validation
failure the previous snapshot is retained and the failure reported to the
meta-channel (Constitution principle V + VI).

A documented `config/example.yaml` ships with the repo and is the contract for ops.

---

## 4. Phase Task List (T1 – T25)

Every task follows TDD per Constitution principle III and SKILL guidance: write a
failing test first, then make it pass, then refactor. Each task is sized 2–5
minutes. Cross-phase dependencies are NOT allowed: a phase MUST be checkpointed
green before the next begins.

**Legend**: `[FR-…]` cites the spec FR(s) the task implements; `[SC-…]` cites the
spec success criteria the task helps verify; `[CP-…]` cites the Constitution
principle(s) the task realises.

### Phase 0 (Setup) — one-time, runs before T1

- [ ] **T0.a** Initialise `pyproject.toml` (poetry); pin Python 3.11; add deps:
      fastapi, uvicorn[standard], httpx, sqlalchemy[asyncio], asyncpg, aiosqlite,
      pydantic[email], pyyaml, structlog, watchdog. Dev deps: pytest,
      pytest-asyncio, pytest-cov, mypy, black, ruff, import-linter.
- [ ] **T0.b** Configure `pyproject.toml` for black + ruff + mypy `--strict`.
- [ ] **T0.c** Add `.importlinter` rule "4-layer dependency direction" and wire
      into CI.
- [ ] **T0.d** Add `.github/workflows/ci.yml`: lint → typecheck → test → coverage
      gate (≥ 80 % on `app/services`, `app/clients`, `app/webhooks`,
      `app/services/oncall.py`, `app/services/cards.py`).

### Phase 1 — US1 (P1) MVP — End-to-end card lifecycle (Week 1)

- [ ] **T1** Lark application registration + `POST /webhook/lark` route that handles
      `type=url_verification` BEFORE any signature step; respond
      `{"challenge": <value>}` within 5 s. *Tests: `tests/integration/signature/test_lark_url_verification.py` — green response on challenge body, MUST run before signature-fail path.* `[FR-004][SC-001][CP-VIII]`
- [ ] **T2** `clients.flashduty` payload parser + `webhooks.flashduty` route:
      verify HMAC signature → parse → dedup-INSERT into `audit_log` (claim-check) →
      `INSERT alerts ON CONFLICT DO NOTHING` (UNIQUE on `incident_fingerprint`).
      *Tests: signature happy/tampered/missing; idempotent on replay.*
      `[FR-001][FR-002][FR-005][CP-II][CP-VII]`
- [ ] **T3** `services.cards.render(firing, alert)` returns Lark card payload
      (severity colour, service, time-in-tz, summary, NO buttons yet) →
      `clients.lark.post_card()` → persist `lark_message_id` on the `alerts` row.
      *Tests: card payload shape against fixtures; severity-colour map.*
      `[FR-006][FR-007][FR-009][SC-001]`
- [ ] **T4** On `incident.closed`, look up `alerts.lark_message_id` →
      `clients.lark.patch_card(message_id, resolved_payload)` → set state =
      `resolved`. *Tests: same `message_id` PATCHed; no second `post_card` call;
      duplicate `closed` events are no-ops.* `[FR-010][FR-021][SC-010]`
- [ ] **T5** Idempotency hard test: 100 redeliveries of the same `incident.created`
      → exactly **one** alerts row, one `lark.post_card` call observed by
      MockTransport. Same for 100 duplicate `incident.closed`. *Test category:
      `tests/integration/idempotency/test_replay_100x.py`.* `[FR-005][SC-003][CP-II]`
- [ ] **T6** `app/config.py` — load YAML, validate with `AlertBotConfig` Pydantic
      model, expose `get_config()`; watchdog-based hot-reload swapping the snapshot
      atomically; validation-failure path keeps old snapshot and reports to
      meta-channel. *Tests: golden YAML loads; malformed YAML rejected; reload on
      file change picked up within 2 s.* `[FR-029][CP-V]`
- [ ] **T7** `app/observability.py` — structlog with JSON renderer;
      `trace_id` ContextVar; FastAPI middleware that mints a trace id per request;
      `MetaChannelReporter` async client (a thin wrapper over `clients.lark` that
      posts to a separate group). Wire all `try/except` re-raisers to the reporter.
      *Tests: trace_id propagation across an end-to-end MockTransport flow;
      reporter called on simulated 5xx.* `[FR-028][CP-VI]`
- [ ] **T8** Multi-stage `Dockerfile` + Helm chart skeleton + `values-staging.yaml`
      → deploy to staging cluster → run a synthetic FlashDuty `incident.created`
      → verify the card appears in the staging Lark group → run synthetic
      `incident.closed` → verify in-place resolve. **Phase 1 checkpoint.**
      `[SC-001][SC-010]`

### Phase 2 — US2 (P1) + US3 (P2) — On-call mention + silence buttons (Week 2)

- [ ] **T9** `services.oncall.resolve(alert) -> OncallTarget` — implements the
      4-tier D-plan: `incident.labels.lark_user` → FlashDuty schedule API → static
      `service → user` map → fallback group role. `services.oncall` owns a 5-minute
      TTL cache for schedule reads (sole permitted polling exception, FR-013 +
      CP-I). *Tests: each tier in isolation; fallthrough on each upstream failure;
      cache hits skip the FD call.* `[FR-012][FR-013][CP-I]`
- [ ] **T10** `clients.lark.lookup_user_email(user_id) -> str | None` and reverse
      `lookup_user_by_email(email)` for @-mention. Cache (per-process, no TTL —
      Lark user_id ↔ email is stable). *Tests: 2xx happy + 404 + 5xx; invalid
      email → meta-channel.* `[FR-018]`
- [ ] **T11** `services.cards.render(firing, alert, oncall_target)` extended with
      6 action buttons and the @-mention. Update T3's tests.
      `[FR-008][FR-014]`
- [ ] **T12** `webhooks/lark.py` extended for `event_callback.card.action.trigger`:
      verify Lark signature (Encrypt Key + Verification Token + timestamp), decrypt
      if encrypted, dedup-INSERT into `audit_log` keyed on Lark `event_id`, parse
      action payload (alert_fingerprint + duration choice + clicker user_id).
      *Tests: signature happy/tampered/missing/encrypted; replay-safe.*
      `[FR-003][FR-005][CP-II][CP-VII]`
- [ ] **T13** `clients.alertmanager.create_silence(matchers, ends_at, created_by)`:
      translate alert labels → AM matchers; POST to `/api/v2/silences`; persist a
      `silences` row (UNIQUE on `alertmanager_silence_id` AND on `lark_event_id`).
      *Tests: matcher translation correctness; createdBy = real email; SQL CHECK
      duration ≤ 24 h passes; idempotent on replayed `lark_event_id`.*
      `[FR-015][FR-017][SC-002][SC-004][SC-008]`
- [ ] **T14** `services.cards.render(silenced, alert, silence)` and PATCH the same
      `lark_message_id` to silenced state showing expiry (in team timezone) and
      operator's display name. UPDATE `alerts.state = silenced`. *Tests:
      message_id equality with the firing card; expiry shown in tz.*
      `[FR-019][FR-010]`
- [ ] **T15** Hard 24-hour cap enforcement at the **router layer** in
      `webhooks/lark.py` (defence-in-depth alongside the SQL CHECK in T13). Any
      duration choice resolving to > 24 h is rejected with 400 to Lark and a card
      inline message; no AM call is attempted. *Tests: `25h` custom rejected
      pre-AM; fixed `24h` accepted; SQL CHECK is the second line of defence.*
      `[FR-016][FR-017][SC-008]`
- [ ] **T16** Failure mode: AM unreachable / 5xx within `httpx` timeout=5s + 3
      retries (exponential backoff). On terminal failure, render an inline
      "Silence failed" notice into the card via `lark.patch_card` and report to
      meta-channel. Do NOT mark the card silenced. *Tests: timeout / 502 / 503 /
      connection-refused all surface the user-friendly notice.*
      `[FR-020][FR-027][FR-028][CP-VI]`
- [ ] **T17** `createdBy` fallback: if Lark `lookup_user_email` returns no email,
      use `lark:<user_id>` and report missing-email to meta-channel; AM silence
      still goes through. *Tests: no-email path produces `lark:<user_id>` value
      visible in AM and audit row.* `[FR-018][SC-004]`

**Phase 2 checkpoint**: a real engineer in staging clicks `[30min]` → AM has a new
silence with `createdBy = <their real email>`; card flips to grey silenced. Manual.

### Phase 3 — US4 (P3) — Custom silence duration (end of Week 2)

- [ ] **T18** `clients.lark.open_form_modal(card_id, fields=[duration])` invocation
      on `[Custom]` click; handle modal-submit callback as a second
      `card.action.trigger` variant. *Tests: modal-open request shape; submit
      callback parsed.*
- [ ] **T19** Duration parser: accept `<int>(min|h)` between `1min` and `24h`
      inclusive; reject `>24h`, `<1min`, malformed, NaN. Reuse T15 for the upper
      bound. *Tests: parametrised valid/invalid table.* `[FR-016][FR-017]`
- [ ] **T20** Wire the parser output: valid → reuse T13–T14 path; invalid → reply
      with an inline error in the modal AND leave the original card in `firing`
      state. *Tests: invalid → no AM call, original card unchanged.*
      `[FR-016][FR-020]`

### Phase 4 — Hardening & Production rollout (Week 3)

- [ ] **T21** SC instrumentation: emit Prometheus metrics for
      (a) `webhook_handler_duration_seconds{route}` p95 → SC-001/002,
      (b) `idempotency_dedup_total` → SC-003,
      (c) `silence_createdBy_real_email_ratio` → SC-004,
      (d) `meta_channel_report_latency_seconds` → SC-007.
      *Tests: histograms emit; metrics endpoint reachable.* `[SC-001..SC-007]`
- [ ] **T22** SC-009 end-to-end test in staging: change `static_service_map` in
      ConfigMap → trigger a synthetic alert → next card mentions the new on-call
      WITHOUT a Pod restart. Documented in [quickstart.md](./quickstart.md).
      `[FR-029][SC-009]`
- [ ] **T23** Production Helm chart values: public Ingress with TLS, certificate
      reference (cert-manager), domain `alertbot.hashkeychain.net`. Sealed Secret
      for Lark/FD/AM credentials. `serviceaccount.yaml` enforces least-privilege.
- [ ] **T24** Documentation: top-level `README.md` (link to spec + plan +
      constitution), `docs/runbook.md` (on-call playbook for AlertBot itself),
      `docs/rollback.md` (`helm rollback` recipe + when to invoke).
- [ ] **T25** Canary: route 1 of N alerts via AlertBot for 24 h on production;
      verify SC-001/002/003/004 metrics; flip to 100 %. **Phase 4 checkpoint.**

---

## 5. Test Matrix

| Component | Unit | Integration (MockTransport) | Idempotency (×100 replay) | Signature | Config | FRs covered | SCs covered |
|---|---|---|---|---|---|---|---|
| `webhooks/flashduty.py` | ✅ parse + verify branches | ✅ end-to-end FIRING / RESOLVED flow | ✅ `incident.created` & `closed` | ✅ happy / tampered / missing | n/a | FR-001 / 002 / 005 / 021 | SC-001 / 003 / 010 |
| `webhooks/lark.py` (url_verification) | ✅ challenge short-circuit | ✅ pre-signature ordering | n/a | ✅ url_verification body bypasses sig | n/a | FR-004 | SC-001 |
| `webhooks/lark.py` (card.action) | ✅ payload parse | ✅ end-to-end SILENCED flow | ✅ same `event_id` × 100 | ✅ encrypted + clear, happy / tampered | n/a | FR-003 / 005 / 015 / 023 | SC-002 / 003 / 004 |
| `services/oncall.py` | ✅ each of 4 tiers | ✅ fallthrough sequence with FD-down | n/a | n/a | ✅ priority order from YAML | FR-012 / 013 | — |
| `services/cards.py` | ✅ render firing/silenced/resolved | ✅ in-place patch via MockTransport | ✅ patch idempotency | n/a | ✅ severity-colour + tz from YAML | FR-006 / 007 / 008 / 010 / 014 / 019 / 021 | SC-001 / 010 |
| `services/audit.py` | ✅ redaction rules | ✅ audit row written for every API call | ✅ duplicate dedup_key → IntegrityError → 200 | n/a | n/a | FR-005 / 025 / 026 | SC-006 |
| `clients/lark.py` | ✅ payload builders | ✅ MockTransport asserts URLs / methods | n/a | n/a | n/a | FR-006 / 007 / 010 / 014 / 019 | SC-001 / 010 |
| `clients/flashduty.py` | ✅ schedule parse | ✅ 5-min cache (no second call) | n/a | n/a | ✅ TTL from YAML | FR-013 | — |
| `clients/alertmanager.py` | ✅ matcher translation | ✅ create_silence happy + 5xx + timeout | n/a | n/a | n/a | FR-015 / 017 / 020 / 027 | SC-002 / 004 / 008 |
| `app/config.py` | ✅ Pydantic schema validation | ✅ hot-reload via tmpfile observer | n/a | n/a | ✅ this is the focus | FR-029 | SC-009 |
| `app/observability.py` | ✅ trace_id ContextVar | ✅ meta-channel reporter on simulated failure | n/a | n/a | n/a | FR-028 | SC-007 |
| `models.py` | ✅ DDL constraints (CHECK ≤ 24 h) | ✅ unique violation paths | ✅ direct DB-level | n/a | n/a | FR-005 / 017 | SC-003 / 008 |

**Coverage gate**: `app/services/*` and `app/webhooks/*` and `app/clients/*` MUST each
hold ≥ 80 % line coverage in CI. CI fails on regression.

**Forbidden in tests**: real network. All HTTP is via `httpx.MockTransport`. All time
is via a `freezegun`-style fixture. All DB tests use SQLite (`:memory:`) for speed.

**Live-sample fixtures**: real captured samples — one each for FlashDuty
`incident.created` / `incident.updated` / `incident.closed` and Lark
`url_verification` / `card.action.trigger` — live in `tests/fixtures/` and are
reloaded as the sole truth source for payload shapes.

---

## 6. Risks & Mitigations

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | AM-silence vs. FD-incident state drift (silenced in AM, still open in FD UI) | High | Low | **Decision applied** (FR-024): AM is sole silence SoT; drift is documented and accepted. Operator runbook explicitly states this. No code mitigation. |
| R2 | Webhook replay → duplicate cards / silences / @-mentions | High | High | DB UNIQUE on `audit_log(event_source, dedup_key)` + `alerts.incident_fingerprint` + `silences.lark_event_id` + 100×-replay test in T5/T13 (CP-II). |
| R3 | Lark `url_verification` handshake fails → app un-installable | Medium | Critical | T1 ships first; route handler matches `type=url_verification` BEFORE signature check; integration test asserts ordering; quickstart.md includes a manual smoke test. |
| R4 | Lark `message_id` lost in DB → state-update event has no card to PATCH | Low | Medium | Persist `(fingerprint, message_id)` in the same transaction as the audit row; on miss, use the documented "Original card lost" fallback path (FR-011) and report to meta-channel. |
| R5 | Alertmanager unreachable → silence button appears "stuck" | Medium | Medium | httpx timeout 5 s + 3-retry exp. backoff (FR-027); on terminal failure, inline failure notice on the card (T16); meta-channel report; no fake "silenced" state. |
| R6 | Operator's Lark profile has no email → `createdBy` cannot be a real email | Low | Medium | `lark:<user_id>` fallback (FR-018); meta-channel notification per occurrence; SC-004 explicitly counts the fallback as compliant. |
| R7 | Malicious / accidental silence of a critical alert | Medium | Medium | 24-h hard cap (FR-017, defence in depth: route layer + SQL CHECK); full `createdBy` audit (FR-025); silences cancellable from AM UI by anyone (FR-022 dropped intentionally). Operator runbook describes how to spot abuse via the audit table. |
| R8 | Config hot-reload introduces a malformed YAML and the service starts using bad config | Low | High | Pydantic validation re-runs on every reload; failure keeps the previous snapshot in memory and reports to meta-channel (CP-V + CP-VI); no live-config replacement on validation failure. |
| R9 | FlashDuty signature scheme details unknown at plan time | High | Low | Resolved in [research.md](./research.md): captured FD docs link + sample payloads in `tests/fixtures/flashduty/`. T2 has signature happy/tampered/missing tests. |
| R10 | Coverage drift over time (modules added without tests) | Medium | Medium | CI gate at 80 % (Constitution Quality Standards); import-linter rule prevents accidental layer violations. |

---

## 7. Constitution Check

> *GATE: must pass before Phase 0 research; re-checked after Phase 1 design.*

Status: **PASS** at both gates. Each Constitution principle maps to concrete plan
artefacts:

| Principle | How this plan satisfies it |
|---|---|
| **I. Webhook-First, Polling-Last** | Inbound: only two HTTP routes, both webhook-receivers (`/webhook/fd`, `/webhook/lark`). The single permitted "pull" path is `clients.flashduty.read_schedule` with a 5-minute TTL cache (FR-013), enforced in `services.oncall`; no other client method polls. T9 verifies the cache hit-path skips the second FD call. |
| **II. Idempotent & Replay-Safe (NON-NEGOTIABLE)** | DB-level UNIQUE constraints: `audit_log (event_source, dedup_key)` + `alerts.incident_fingerprint` + `silences.alertmanager_silence_id` + `silences.lark_event_id`. T5 and T13 each include a 100×-replay test. The audit-log claim-check pattern (T2/T12) ensures duplicate webhooks short-circuit before any business logic runs. |
| **III. Test-First Development (NON-NEGOTIABLE)** | Every task in T1–T25 explicitly lists the failing test to write first. CI gate (`T0.d`) refuses merges on coverage regression below 80 % on `services / clients / webhooks`. mypy `--strict`, black, ruff are blocking. |
| **IV. Audit Everything** | `app/services/audit.py` is the single write path for `audit_log`. Every webhook receipt (T2, T12), every outbound API call (lark.post_card, lark.patch_card, alertmanager.create_silence — T3, T4, T13, T14), and every button click (T12) goes through it. SC-006 is instrumented in T21 with the `idempotency_dedup_total` metric. Audit-write failure does not block business path (FR-026). |
| **V. Config-Driven, Not Hardcoded** | `app/config.py` (T6) loads YAML, validates via `AlertBotConfig` Pydantic v2 model, and hot-reloads via watchdog. **No** business literal (severity colours, oncall priority order, button durations, fallback role, timezone, max-silence-hours) appears in Python code. T22 verifies hot-reload end-to-end (SC-009). |
| **VI. Fail Fast & Visible** | `MetaChannelReporter` (T7) is invoked from every `try/except` re-raise site and from every terminal outbound-failure path (T16). Silent `try/except` is forbidden by code review and is also visually obvious because every catch site that re-raises must call the reporter. SC-007 instruments the latency target. |
| **VII. Verify Every Webhook (NON-NEGOTIABLE)** | Both inbound routes verify signatures before any DB write or business call: T2 (FlashDuty HMAC) and T12 (Lark Encrypt-Key + Verification-Token + timestamp). Test category `tests/integration/signature/` covers happy + tampered + missing for both, and asserts HTTP 401 on failure. |
| **VIII. Lark URL Verification First** | T1 ships the `type=url_verification` handler as the first matching branch in `webhooks/lark.py`, BEFORE the signature-verification step. An integration test (T1) explicitly asserts the ordering: a handshake body with no signature gets `200 {"challenge": …}`, NOT `401`. |

**Tech-stack check** (Constitution "Tech Stack Constraints"): every dependency in
`pyproject.toml` (T0.a) is on the allow-list. Forbidden libraries (LangChain,
LangGraph, vector DBs, MCP client, Redis, message queues, Celery) are explicitly
absent. Import-linter rule (T0.c) enforces no transitive pull-in.

**Tech-stack tradeoffs**: none. Plan uses only Constitution-approved libraries.

---

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| _(none — this plan introduces no Constitution violations.)_ | — | — |

The plan stays within all Constitution constraints: 4-layer monolith, no MQ, no
Redis, no extra projects, no new languages. The single design choice that warrants
explicit explanation is folding webhook dedup into the `audit_log` table rather than
introducing a fourth `webhook_events` table — see §2.4. This is a simplification, not
a complexity addition.

---

## Phase Execution Notes

- **Phase 0 (research)**: see [research.md](./research.md). All open
  technical-detail items resolved (FlashDuty signature scheme, Lark form modal API,
  config hot-reload library choice).
- **Phase 1 (design & contracts)**: see [data-model.md](./data-model.md) and
  [contracts/](./contracts/). Quickstart for local validation:
  [quickstart.md](./quickstart.md).
- **Phase 2 (tasks)**: T1–T25 above; the `/speckit.tasks` command will lift these
  into `tasks.md` with proper `[P]` parallelism markers and `[Story]` mapping.

**Stop point**: this plan ends after Phase 1 outputs. Generation of `tasks.md` is the
job of `/speckit.tasks`, not `/speckit.plan`.
