# Tasks: AlertBot Core

**Input**: Design documents from `/specs/001-alertbot-core/`
**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [data-model.md](./data-model.md),
[contracts/](./contracts/), [research.md](./research.md), [quickstart.md](./quickstart.md)
**Constitution**: AlertBot Constitution v1.0.0 (`.specify/memory/constitution.md`)

**Tests**: TESTS ARE MANDATORY (Constitution principle III + spec FR-005). Every user
story phase begins with failing tests before any implementation task starts.

**Organization**: tasks are grouped by user story so each story can be implemented,
tested, and demoed independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelisable — different files, no dependencies on incomplete tasks.
- **[Story]**: present on user-story-phase tasks only; absent on Setup / Foundational
  / Polish phases.
- Every task names an exact file path.
- Every task is sized 2–5 minutes (one focused TDD cycle).

## Path Conventions (from plan.md §1.3)

- `app/` — Python source (4-layer architecture: `webhooks/` → `services/` → `clients/` → `models.py`; cross-cutting `config.py`, `observability.py`, `main.py`).
- `tests/{unit,integration,fixtures}/` — mirrors `app/`; integration tests use `httpx.MockTransport`.
- `deploy/` — Dockerfile + Helm chart.
- `config/` — YAML config samples.
- `scripts/` — local-dev helpers (`fd-fire.py` etc., per quickstart.md).

## Plan-task ↔ tasks-file mapping

The plan groups work as T1–T25 by phase. tasks.md re-numbers as T001–T073 with strict
checklist format. Cross-reference is preserved in each task description (`(plan: Tx)`).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: project skeleton + tooling. Everything downstream is blocked on this.

- [X] T001 Initialise `pyproject.toml` (poetry); pin Python 3.11; add deps `fastapi`, `uvicorn[standard]`, `httpx`, `sqlalchemy[asyncio]`, `asyncpg`, `aiosqlite`, `pydantic[email]`, `pyyaml`, `structlog`, `watchdog`; dev deps `pytest`, `pytest-asyncio`, `pytest-cov`, `mypy`, `black`, `ruff`, `import-linter`, `freezegun` in `pyproject.toml` (plan: T0.a)
- [X] T002 [P] Configure `black` line-length 100, `ruff` rules (E,F,I,N,UP,B,SIM), `mypy --strict` in `pyproject.toml` (plan: T0.b)
- [X] T003 [P] Add 4-layer dependency contract in `.importlinter` per research.md §7 (plan: T0.c)
- [X] T004 [P] Add `.github/workflows/ci.yml` with `lint → typecheck → test → coverage` gate (≥ 80 % on `app/services`, `app/clients`, `app/webhooks`) (plan: T0.d)
- [X] T005 [P] Create directory skeleton under repo root: `app/{webhooks,services,clients}/`, `app/{__init__,main,config,observability,models}.py` placeholders, `tests/{unit,integration,fixtures}/{flashduty,lark}/`, `config/`, `scripts/`, `deploy/helm/alertbot/templates/`
- [X] T006 [P] Add `.gitignore` (Python + venv + `.env*` + `alertbot.db*` + `.coverage*`) and `.dockerignore` at repo root

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: cross-cutting modules every user story depends on — DB models, audit
service, config loader, observability. **No user-story work may start until Phase 2
completes (checkpoint at the end).**

### Foundational tests (write FIRST, ensure FAIL)

- [X] T007 [P] Write SQLAlchemy DDL constraint tests (`alerts.incident_fingerprint UNIQUE`; `silences.alertmanager_silence_id UNIQUE`; `silences.lark_event_id UNIQUE`; `silences` 24-hour CHECK; `audit_log (event_source, dedup_key)` UNIQUE) in `tests/unit/models/test_constraints.py`
- [X] T008 [P] Write `app/services/audit.py::record()` claim-check test (duplicate `(event_source, dedup_key)` raises `IntegrityError`) in `tests/unit/services/test_audit_dedup.py`
- [X] T009 [P] Write `app/config.py` Pydantic schema test: golden YAML loads, malformed YAML rejected, `max_silence_hours > 24` rejected by `Field(le=24)` in `tests/unit/test_config_schema.py`
- [X] T010 [P] Write `app/config.py` hot-reload test: write a tmpfile → mutate → next `get_config()` returns new value within 2 s; bad YAML → snapshot unchanged + meta-channel called in `tests/integration/config/test_hot_reload.py`
- [X] T011 [P] Write `app/observability.py` trace_id propagation test: middleware mints id → log lines + audit rows + meta-channel reports all carry the same id in `tests/unit/test_observability.py`

### Foundational implementation

- [X] T012 Create `Alert`, `Silence`, `AuditLog` SQLAlchemy 2.0 async models with all UNIQUE / CHECK constraints per [data-model.md](./data-model.md) in `app/models.py`
- [X] T013 [P] Create `make_engine()` and `make_session_factory()` async factories at the bottom of `app/models.py`
- [X] T014 [P] Initialise Alembic (`alembic init -t async migrations`) and write the initial migration that creates all three tables in `migrations/versions/0001_initial_schema.py`
- [X] T015 Create `AlertBotConfig` (+ `LarkConfig`, `FlashdutyConfig`, `AlertmanagerConfig`, `OncallConfig`, `SilenceButtonsConfig`) Pydantic v2 models per plan §3 in `app/config.py`
- [X] T016 Implement YAML loader + atomic snapshot swap (`get_config()` singleton, `RLock`-protected) in `app/config.py`
- [X] T017 Implement watchdog-based hot-reload + meta-channel notification on validation failure in `app/config.py` (plan: T6)
- [X] T018 [P] Configure structlog (`add_log_level → TimeStamper → merge_contextvars → JSONRenderer`) per research.md §10 in `app/observability.py`
- [X] T019 [P] Add `trace_id` `ContextVar` and FastAPI middleware that mints one per request in `app/observability.py`
- [X] T020 Implement `MetaChannelReporter` (direct `httpx.AsyncClient` to Lark, bypassing `clients/lark.py` to keep cross-cutting) in `app/observability.py` (plan: T7)
- [X] T021 [P] Implement `redact()` helper (mask `app_secret`, `token`, `authorization`, `encrypt_key`; truncate `fingerprint`) in `app/observability.py`
- [X] T022 Implement `app/services/audit.py::record(event_source, dedup_key, operation, …)` write-with-dedup gateway (claim-check pattern; `IntegrityError → False` return; non-blocking on audit-write failure per FR-026) in `app/services/audit.py`
- [X] T023 [P] Capture real FlashDuty webhook samples (`incident.created.json`, `incident.updated.json`, `incident.closed.json`) in `tests/fixtures/flashduty/`
- [X] T024 [P] Capture real Lark samples (`url_verification.json`, `card_action_trigger.json`, `card_action_trigger_encrypted.json`) in `tests/fixtures/lark/`
- [X] T025 [P] Set up shared pytest fixtures: `db_session`, `mock_transport`, `frozen_time`, `mock_meta_reporter` in `tests/conftest.py`
- [X] T026 Create FastAPI app factory with lifespan (DB engine startup/shutdown, hot-reload watcher, structlog binding) in `app/main.py`

**Phase 2 checkpoint**: Phase 2 tests T007–T011 all green; CI on the branch shows lint + typecheck + tests + coverage all green; `import-linter` clean. User-story work may now begin.

---

## Phase 3: User Story 1 — Alerts visible in Lark cards (Priority: P1) 🎯 MVP

**Goal**: a synthetic FlashDuty `incident.created` produces a severity-coloured Lark card; an `incident.closed` updates the same card to `resolved`. Idempotent on replay.

**Independent Test**: per spec US1 — fire a test FD `incident.created` → verify card appears within 5 s with correct severity / service / time / summary; fire `incident.closed` → same `message_id` is patched to `resolved`; replay original 100× → exactly one card and one alert row.

### Tests for US1 (write FIRST — must FAIL before any implementation task in this phase)

- [X] T027 [P] [US1] FD signature happy / tampered-body / missing-header / stale-timestamp tests in `tests/integration/signature/test_fd_signature.py`
- [X] T028 [P] [US1] Lark `url_verification` handshake test — asserts handler returns `{"challenge": …}` BEFORE the signature-verification step (CP-VIII regression) in `tests/integration/signature/test_lark_url_verification.py`
- [X] T029 [P] [US1] Card-payload unit test for firing state (severity → colour map; service / time-in-tz / summary present; NO buttons; NO @-mention yet) using `tests/fixtures/flashduty/incident.created.json` in `tests/unit/services/test_cards_firing_payload.py`
- [X] T030 [P] [US1] FIRING flow integration test: `incident.created` → MockTransport asserts `lark.post_card` called once with expected payload; `alerts` row created with correct `lark_message_id` in `tests/integration/flows/test_us1_firing.py`
- [X] T031 [P] [US1] RESOLVED flow integration test: `incident.closed` for an existing alert → MockTransport asserts `lark.patch_card` called with the SAME `message_id`; `alerts.state = resolved`; no new card posted (SC-010) in `tests/integration/flows/test_us1_resolved.py`
- [X] T032 [P] [US1] 100× replay idempotency test: same `incident.created` payload × 100 → exactly one `alerts` row, exactly one `lark.post_card` MockTransport call, 99 audit rows for replay short-circuits (SC-003) in `tests/integration/idempotency/test_us1_replay.py`

### Implementation for US1

- [X] T033 [P] [US1] FlashDuty webhook payload Pydantic models (`FlashDutyEvent`, `Incident`) per [contracts/inbound-flashduty.md](./contracts/inbound-flashduty.md) in `app/clients/flashduty.py`
- [X] T034 [P] [US1] `verify_fd_signature(headers, body)` HMAC-SHA256 over `"<ts>.<body>"` + 5-min replay window (research.md §1) in `app/clients/flashduty.py`
- [X] T035 [P] [US1] `clients/lark.py::post_card(chat_id, card_payload) -> message_id` with 5 s timeout + 3-retry exp backoff per [contracts/outbound-lark.md](./contracts/outbound-lark.md) in `app/clients/lark.py`
- [X] T036 [P] [US1] `clients/lark.py::patch_card(message_id, card_payload)` with 404 → `[Original card lost]` fallback (FR-011) in `app/clients/lark.py`
- [X] T037 [P] [US1] `services/cards.py::render_firing(alert) -> dict` — severity colour map, service, time-in-tz, summary; **no** buttons / @-mention yet in `app/services/cards.py`
- [X] T038 [P] [US1] `services/cards.py::render_resolved(alert) -> dict` — green title bar, resolution time, original summary in `app/services/cards.py`
- [X] T039 [US1] `webhooks/flashduty.py::handle()` route: parse → verify (T034) → audit dedup INSERT via `services.audit.record()` → on duplicate return 200 → on first delivery dispatch by `event_type` (depends on T012 / T022 / T034) in `app/webhooks/flashduty.py` (plan: T2)
- [X] T040 [US1] `webhooks/lark.py::handle()` route — `url_verification` MUST be the first matched branch, BEFORE signature verification (CP-VIII / FR-004) in `app/webhooks/lark.py` (plan: T1)
- [X] T041 [US1] `services/cards.py::handle_firing(incident)` — render → `clients.lark.post_card` → INSERT alert row in same transaction as the audit row (depends on T037 / T035) (plan: T3)
- [X] T042 [US1] `services/cards.py::handle_resolved(incident)` — SELECT alert by `incident_fingerprint` → `clients.lark.patch_card(message_id, render_resolved())` → UPDATE state to `resolved` (depends on T038 / T036) (plan: T4)
- [X] T043 [US1] Wire `webhooks/flashduty.py` dispatcher: `incident.created → handle_firing`; `incident.updated → handle_update` (stub for now); `incident.closed → handle_resolved` in `app/webhooks/flashduty.py`
- [X] T044 [US1] Add example YAML config `config/example.yaml` documenting every key (severity colours, timezone, fallback role, etc.) per quickstart §2
- [X] T045 [US1] Multi-stage `Dockerfile` (builder: poetry install; runtime: slim, non-root user, `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0"]`) in `deploy/Dockerfile`
- [X] T046 [US1] Helm chart skeleton + `values-staging.yaml` (Deployment, Service, Ingress, ConfigMap for YAML, SealedSecret for credentials, ServiceAccount) in `deploy/helm/alertbot/templates/` and `deploy/helm/alertbot/values-staging.yaml`
- [ ] T047 [US1] Manual staging smoke test per quickstart §5.2 + §5.5 (fire synthetic FD `incident.created` → card → fire `incident.closed` → resolved). Documented sign-off in PR.

**Phase 3 checkpoint**: US1 fully testable — alerts visible in Lark, idempotent, in-place resolve. **MVP shippable here.**

---

## Phase 4: User Story 2 — On-call attribution (Priority: P1)

**Goal**: the firing card @-mentions the engineer the D-plan resolves to, with a 5-min schedule cache.

**Independent Test**: per spec US2 — for each of the 4 D-plan tiers, configure exactly that tier (others unset/down) → fire alert → verify the card mentions the expected user; second alert within 5 min produces zero additional FD-schedule API calls.

### Tests for US2 (write FIRST)

- [X] T048 [P] [US2] D-plan priority chain unit test — each of 4 tiers in isolation; explicit label wins over schedule; schedule wins over static map; fallback role in `tests/unit/services/test_oncall_priority.py`
- [X] T049 [P] [US2] Fall-through test: FD schedule API down → falls to static map; static map empty → falls to fallback role; FR-006 meta-channel called for the FD failure in `tests/unit/services/test_oncall_fallthrough.py`
- [X] T050 [P] [US2] 5-min cache test: two `read_schedule(service)` calls within 5 min → exactly one MockTransport hit; calls > 5 min apart → two hits (FR-013) in `tests/integration/flows/test_oncall_cache.py`
- [X] T051 [P] [US2] Card @-mention rendering test: each oncall tier output renders to the correct Lark **interactive-card `lark_md`** mention syntax — `<at id=ou_xxx></at>` for user (open_id/user_id; tag body MUST be empty — Lark renders the name from the id), `<at email=…></at>` for email-only fallback, or role text for fallback role — in `tests/unit/services/test_cards_mention.py`. NOTE: this is **different** from the IM message-body syntax `<at user_id="…">Name</at>`; the latter is silently dropped inside a `lark_md` text element. Refs: https://open.feishu.cn/document/common-capabilities/message-card/message-cards-content/using-markdown-tags
- [X] T052 [P] [US2] **Surface guard regression test (FR-024)**: import `app.clients.flashduty` and assert its public surface is exactly `{read_schedule, parse_webhook, verify_fd_signature, FlashDutyEvent, Incident}` — no `ack`, `close`, `snooze`, `update_incident` callable in `tests/unit/clients/test_flashduty_readonly.py`

### Implementation for US2

- [X] T053 [P] [US2] `clients/flashduty.py::read_schedule(service) -> str | None` — GET `/api/v1/schedules?service=…&now=true`; **READ-ONLY** (FR-024) in `app/clients/flashduty.py`
- [X] T054 [P] [US2] In-process 5-min TTL cache wrapper around `read_schedule` (per-service key) (FR-013) in `app/clients/flashduty.py`
- [X] T055 [P] [US2] `clients/lark.py::lookup_user_email(user_id) -> str | None` — GET `/open-apis/contact/v3/users/{user_id}` with no-TTL per-process cache (research note: user_id↔email is stable) in `app/clients/lark.py`
- [X] T056 [P] [US2] `clients/lark.py::lookup_user_by_email(email) -> str | None` (reverse direction, used for `@-mention` from a static map entry) in `app/clients/lark.py`
- [X] T057 [US2] `services/oncall.py::resolve(alert) -> OncallTarget` — 4-tier D-plan walker (`incident.labels.lark_user → fd_schedule → static_map → fallback_role`); reads priority order from config (depends on T053 / T054 / T055 / T056) in `app/services/oncall.py` (plan: T9)
- [X] T058 [US2] Extend `services/cards.py::render_firing(alert, oncall_target)` with `@-mention` block in card body (depends on T037 / T057) in `app/services/cards.py` (plan: T11a)
- [X] T059 [US2] Wire `services.oncall.resolve()` into `services.cards.handle_firing()` so every new firing card is rendered with the resolved on-call (depends on T041 / T057 / T058) in `app/services/cards.py`

**Phase 4 checkpoint**: each D-plan tier verified end-to-end; cache hit rate is observable via MockTransport call counts; FR-024 regression guard locked in.

---

## Phase 5: User Story 3 — One-click silence with real-operator attribution (Priority: P2)

**Goal**: tap `[5min/30min/1h/4h/24h]` → AM silence with `createdBy = real engineer email` → same card flips to grey `silenced` with expiry + operator name. AM unreachable → user-visible failure (no fake `silenced` state).

**Independent Test**: per spec US3 — engineer taps `[30min]` → AM has new silence within 3 s with `createdBy = <real email>`, `endsAt ≈ now + 30 min`, matchers from alert labels; same card patches to silenced; replay of same Lark `event_id` × 100 produces no second silence.

### Tests for US3 (write FIRST)

- [X] T060 [P] [US3] Lark signature test: happy clear-text + happy AES-encrypted + tampered + missing + stale → all paths land at 401 except happy in `tests/integration/signature/test_lark_signature.py`
- [X] T061 [P] [US3] `card.action.trigger` payload-parse unit test: extract `alert_fingerprint` + `duration` + `operator.user_id` correctly from fixture in `tests/unit/webhooks/test_lark_action_payload.py`
- [X] T062 [P] [US3] AM matcher-translation unit test: `{alertname:HighCPU, instance:web-01, lark_user:alice@…}` → matchers list with `lark_user` key excluded (per [contracts/outbound-alertmanager.md](./contracts/outbound-alertmanager.md)) in `tests/unit/clients/test_alertmanager_matchers.py`
- [X] T063 [P] [US3] Silenced-card render unit test: grey title bar, expiry shown in team timezone, operator display name visible, NO action buttons in `tests/unit/services/test_cards_silenced.py`
- [X] T064 [P] [US3] DB CHECK constraint regression test: direct INSERT of a silence with `(ends_at - starts_at) > 24h` fails with `IntegrityError` in `tests/unit/models/test_silences_24h_check.py`
- [X] T065 [P] [US3] AM unreachable failure-mode integration test: MockTransport returns 502 thrice → terminal failure path → inline failure card rendered → meta-channel called → `silences` table empty in `tests/integration/flows/test_silence_failure.py`
- [X] T066 [P] [US3] AM timeout failure-mode test: MockTransport delays > 5 s → `httpx.TimeoutException` → 3 retries observed → terminal failure → same fallback path as T065 in `tests/integration/flows/test_silence_timeout.py`
- [X] T067 [P] [US3] Missing-email fallback test: Lark `lookup_user_email` returns `None` → AM silence is created with `createdBy = "lark:<user_id>"` → meta-channel called for the missing email → `silences.created_by` matches in `tests/integration/flows/test_silence_no_email.py`
- [X] T068 [P] [US3] 100× replay idempotency test: same Lark `card.action.trigger` `event_id` × 100 → exactly one `silences` row, exactly one `alertmanager.create_silence` MockTransport call (SC-003) in `tests/integration/idempotency/test_lark_replay.py`
- [X] T069 [P] [US3] **Auth-absence regression test (FR-023)**: assert `app/webhooks/lark.py` source contains zero references to substrings `is_oncall`, `is_admin`, `role_check`, `permission` (other than docstrings/comments referencing FR-023 explicitly) in `tests/unit/webhooks/test_lark_no_auth_check.py`
- [X] T070 [P] [US3] End-to-end SILENCED flow test: receive `card.action.trigger[30min]` for an existing firing alert → AM `create_silence` called → `silences` row + audit row → `lark.patch_card` called with same `message_id` → `alerts.state = silenced` (SC-002 / SC-004) in `tests/integration/flows/test_us3_silenced.py`

### Implementation for US3

- [X] T071 [P] [US3] Lark signature verifier (HMAC over Verification-Token + ts + nonce + body; AES decryption with Encrypt Key when payload is encrypted) per [contracts/inbound-lark.md](./contracts/inbound-lark.md) in `app/clients/lark.py`
- [X] T072 [P] [US3] Lark `card.action.trigger` payload Pydantic model (header + event.operator + event.action.value with `kind`, `alert_fingerprint`, `duration`) in `app/clients/lark.py`
- [X] T073 [P] [US3] `clients/alertmanager.py::create_silence(matchers, starts_at, ends_at, created_by, comment) -> silence_id` with timeout 5 s + 3-retry exp backoff (retry only on timeout / connect-error / 5xx) per research.md §9 + [contracts/outbound-alertmanager.md](./contracts/outbound-alertmanager.md) in `app/clients/alertmanager.py`
- [X] T074 [P] [US3] `clients/alertmanager.py::matchers_from_labels(labels)` — exact-match matchers excluding `{lark_user, flashduty_team}` (configurable) in `app/clients/alertmanager.py`
- [X] T075 [P] [US3] `services/cards.py::render_firing` extended with 6 action buttons (`5min`, `30min`, `1h`, `4h`, `24h`, `Custom`); each button's `value` carries `kind=silence|custom_open` + `alert_fingerprint` + `duration` (depends on T058) in `app/services/cards.py` (plan: T11b)
- [X] T076 [P] [US3] `services/cards.py::render_silenced(alert, silence)` — grey card with expiry-in-tz + operator display name (depends on T037) in `app/services/cards.py`
- [X] T077 [P] [US3] `services/cards.py::render_silence_failed(alert, reason)` — inline red banner on the original card content; no fake silenced state in `app/services/cards.py`
- [X] T078 [US3] `webhooks/lark.py` extension for `card.action.trigger`: signature verify (T071) → audit dedup INSERT keyed on `header.event_id` → parse → dispatch (depends on T022 / T071 / T072) in `app/webhooks/lark.py` (plan: T12)
- [X] T079 [US3] **Route-layer 24-hour cap rejection** (FR-017 defence-in-depth: route layer before any AM call; SQL CHECK is the second line of defence) — any duration resolving to > 24 h returns 400 + inline card error in `app/webhooks/lark.py` (plan: T15)
- [X] T080 [US3] `services/cards.py::handle_silence_click(operator, alert, duration)` — `lookup_user_email(operator)` → `clients.alertmanager.create_silence(...)` → INSERT silence row → UPDATE alert.state → `lark.patch_card(message_id, render_silenced(...))` (depends on T055 / T073 / T076) in `app/services/cards.py` (plan: T13 / T14)
- [X] T081 [US3] Missing-email fallback: when `lookup_user_email` returns `None` → use `createdBy = "lark:<user_id>"` → meta-channel-report missing-email; AM silence still proceeds (FR-018) in `app/services/cards.py` (plan: T17)
- [X] T082 [US3] AM-unreachable failure path: catch terminal failure from `create_silence` → `lark.patch_card(message_id, render_silence_failed(...))` → meta-channel-report; do NOT mark alert silenced; do NOT INSERT silence row (FR-020) in `app/services/cards.py` (plan: T16)

**Phase 5 checkpoint**: real engineer in staging clicks `[30min]` on a real card → AM has a silence with `createdBy = <their real email>` → card flips silenced. Manual sign-off captured in PR.

---

## Phase 6: User Story 4 — Custom silence duration (Priority: P3)

**Goal**: tap `[Custom]` → Lark form modal opens → enter duration ≤ 24 h → silence created identically to US3. Invalid input rejected with inline error; > 24 h rejected with cap message.

**Independent Test**: per spec US4 — tap `[Custom]` → submit `7h` → silence with `endsAt ≈ now + 7 h`, card flips silenced; submit `25h` → form rejected with cap message, original card stays firing; submit `"banana"` → form rejected with parse-error message.

### Tests for US4 (write FIRST)

- [X] T083 [P] [US4] Duration parser parametrised table test: valid `1min` … `24h`; invalid `0min`, `25h`, `"banana"`, empty, NaN; valid units only `min` and `h` in `tests/unit/services/test_duration_parser.py`
- [X] T084 [P] [US4] Custom 7 h end-to-end flow: tap `[Custom]` → modal opens → submit `7h` → AM silence with `endsAt ≈ now + 7 h` (FR-016) in `tests/integration/flows/test_us4_custom_7h.py`
- [X] T085 [P] [US4] 25 h rejection: form submit with `25h` → 400 + inline modal error → no AM call → `silences` empty → original card unchanged (FR-016 + FR-017) in `tests/integration/flows/test_us4_25h_rejected.py`
- [X] T086 [P] [US4] Malformed duration: form submit with `"banana"` → 400 + inline parse-error → no AM call (FR-016) in `tests/integration/flows/test_us4_malformed.py`

### Implementation for US4

- [X] T087 [P] [US4] `services/cards.py::parse_duration(value: str) -> timedelta` — accept `<int>(min|h)`, range 1 min .. 24 h inclusive; raise `ValueError` on invalid (research.md §3 confirms parser scope) in `app/services/cards.py` (plan: T19)
- [X] T088 [P] [US4] `clients/lark.py::open_form_modal(card_id, fields=[duration])` — POST to Lark form-modal endpoint per [contracts/outbound-lark.md](./contracts/outbound-lark.md) in `app/clients/lark.py` (plan: T18)
- [X] T089 [US4] Wire `[Custom]` button (`value.kind = "custom_open"`) → `clients.lark.open_form_modal(...)` in `app/webhooks/lark.py` dispatcher (depends on T078 / T088)
- [X] T090 [US4] Wire form-submit callback (also arrives as `card.action.trigger` with `value.kind = "silence"` and a parsed `duration`) → reuse `handle_silence_click` (T080); on `parse_duration` failure return 400 + inline modal error message (depends on T080 / T087) in `app/webhooks/lark.py` (plan: T20)

**Phase 6 checkpoint**: 7 h custom flows; 25 h rejected with cap message; malformed input rejected with parse error. SC-008 verified by sample query.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: production-grade observability, hot-reload validation, prod rollout.

- [X] T091 [P] Add Prometheus metrics: `webhook_handler_duration_seconds{route}` (histogram) for SC-001/002; `idempotency_dedup_total{event_source}` counter for SC-003; `silence_created_by_real_email_total` and `silence_created_by_lark_id_total` counters for SC-004; `meta_channel_report_latency_seconds` histogram for SC-007 in `app/observability.py` and per-route middleware (plan: T21)
- [X] T092 [P] Add `GET /metrics` Prometheus endpoint to `app/main.py`
- [X] T093 [P] SC-009 hot-reload end-to-end staging test (per quickstart §5.7): change `static_service_map` in ConfigMap → next alert mentions new email with no Pod restart; documented in `docs/operating/hot-reload.md` and asserted in `tests/integration/config/test_hot_reload_e2e.py` (plan: T22)
- [X] T094 [P] Production Helm values: public Ingress with TLS via cert-manager, domain `alertbot.hashkeychain.net`, SealedSecret for Lark/FD/AM creds, ServiceAccount with least-privilege scopes in `deploy/helm/alertbot/values-prod.yaml` (plan: T23)
- [X] T095 [P] Top-level `README.md` linking to constitution + spec + plan + quickstart
- [X] T096 [P] On-call runbook (how to inspect audit log, how to spot silence abuse, how to read meta-channel reports, how to rotate credentials) in `docs/runbook.md` (plan: T24)
- [X] T097 [P] Rollback playbook (`helm rollback alertbot <rev>`; manual silence-cleanup recipe; DB rollback steps) in `docs/rollback.md` (plan: T24)
- [X] T098 Coverage gate verification: `pytest --cov=app --cov-fail-under=80` shows ≥ 80 % on `app/services/*`, `app/clients/*`, `app/webhooks/*`; CI gate confirmed
- [ ] T099 Production canary rollout: route 1/N alerts via AlertBot for 24 h on production → verify SC-001/002/003/004 metrics on Grafana → flip to 100 %. Rollback recipe ready (plan: T25)
- [X] T100 Final acceptance review against spec.md User Stories US1–US4 + Success Criteria SC-001 .. SC-010 + Constitution v1.0.0 principles I–VIII; sign-off in PR description

---

## Dependencies & Execution Order

### Phase dependencies

- **Phase 1 (Setup)**: no dependencies; T001 unblocks T002–T006 (which are all `[P]`).
- **Phase 2 (Foundational)**: depends on Phase 1; **BLOCKS all user stories**. Within Phase 2, T012 (models) blocks T013/T014/T022; T015 (config schema) blocks T016/T017; T018/T019 block T020; T026 (app factory) is the final integration step.
- **Phase 3 (US1)**: depends on Phase 2 fully complete. Cannot start before T026 is green.
- **Phase 4 (US2)**: depends on Phase 3 complete (US2 extends the firing card built in US1).
- **Phase 5 (US3)**: depends on Phase 3 complete (US3 attaches buttons to the firing card). **Independent of Phase 4** — US3 can be developed in parallel with US2 if staffed.
- **Phase 6 (US4)**: depends on Phase 5 complete (US4 reuses the silence flow).
- **Phase 7 (Polish)**: depends on US3 at minimum (instrumentation needs the silence flow); ideally all stories.

### Within each user story phase

- **Tests MUST be written first and MUST FAIL before any implementation task in the same phase starts** (Constitution III).
- Models / Pydantic schemas before clients.
- Clients before services.
- Services before webhook routes (the dispatch layer).
- All within-phase tests green is the phase checkpoint.

### Parallel opportunities

- All Phase 1 `[P]` tasks: T002–T006 in parallel after T001.
- All Phase 2 foundational tests: T007–T011 in parallel.
- All Phase 2 `[P]` implementation tasks where shown.
- Within US1: T027–T032 (tests) all in parallel; T033–T038 (clients/services) mostly in parallel; T039–T043 (routes/orchestration) sequential.
- Across stories after Phase 3: US2 (Phase 4) and US3 (Phase 5) can be split between two developers.

### File-conflict map (where `[P]` is NOT safe)

| File | Tasks that touch it | Order |
|---|---|---|
| `app/models.py` | T012 → T013 | sequential within file |
| `app/config.py` | T015 → T016 → T017 | sequential |
| `app/observability.py` | T018 / T019 / T020 / T021 — different functions, can be `[P]` | safe |
| `app/services/cards.py` | T037 → T038 → T041 → T042 → T058 → T059 → T075 → T076 → T077 → T080 → T081 → T082 → T087 | sequential within file |
| `app/services/oncall.py` | T057 only | n/a |
| `app/clients/flashduty.py` | T033 / T034 → T053 / T054 — different functions, safe `[P]` once stub exists | safe after T033 |
| `app/clients/lark.py` | T035 / T036 / T055 / T056 / T071 / T072 / T088 — different functions | safe |
| `app/clients/alertmanager.py` | T073 / T074 — different functions | safe |
| `app/webhooks/flashduty.py` | T039 → T043 | sequential |
| `app/webhooks/lark.py` | T040 → T078 → T079 → T089 → T090 | sequential |

---

## Parallel Example: User Story 1 test-first wave

```bash
# Once Phase 2 checkpoint is green, launch US1 tests in parallel:
Task: "T027 [US1] FD signature happy/tampered/missing/stale tests in tests/integration/signature/test_fd_signature.py"
Task: "T028 [US1] Lark url_verification handshake test (asserts BEFORE signature) in tests/integration/signature/test_lark_url_verification.py"
Task: "T029 [US1] Card-payload unit test for firing state in tests/unit/services/test_cards_firing_payload.py"
Task: "T030 [US1] FIRING flow integration test in tests/integration/flows/test_us1_firing.py"
Task: "T031 [US1] RESOLVED flow integration test in tests/integration/flows/test_us1_resolved.py"
Task: "T032 [US1] 100× replay idempotency test in tests/integration/idempotency/test_us1_replay.py"

# Verify all six are RED. Then launch the parallelisable implementation wave:
Task: "T033 [US1] FlashDuty Pydantic models in app/clients/flashduty.py"
Task: "T034 [US1] FD signature verifier in app/clients/flashduty.py"        # same file as T033 — sequential
Task: "T035 [US1] Lark post_card client in app/clients/lark.py"
Task: "T036 [US1] Lark patch_card client in app/clients/lark.py"            # same file — sequential
Task: "T037 [US1] Card firing renderer in app/services/cards.py"
Task: "T038 [US1] Card resolved renderer in app/services/cards.py"          # same file — sequential
```

---

## Implementation Strategy

### MVP (User Story 1 only)

1. Phase 1 Setup → T001 .. T006
2. Phase 2 Foundational → T007 .. T026 (this is the longest single block; budget Week 0 + half of Week 1)
3. Phase 3 US1 → T027 .. T047
4. **STOP & VALIDATE**: alerts visible in Lark with idempotency and resolve. Manual smoke per quickstart §5.2 + §5.5. SC-001 + SC-003 + SC-010 measurable.
5. **MVP shippable to staging here.**

### Incremental delivery

- After MVP: add US2 (Phase 4) → demo @-mention; SC-001 unchanged, SC-006 expanded.
- Then US3 (Phase 5) → demo one-click silence; SC-002 + SC-004 + SC-008 measurable.
- Then US4 (Phase 6) → custom-duration polish.
- Then Polish (Phase 7) → production-grade rollout; SC-007 + SC-009 measurable.

### Parallel team strategy

With two developers and Phase 2 complete:

1. **Dev A**: Phase 3 US1 (T027 .. T047) — owns the FIRING / RESOLVED flow.
2. **Dev B**: drafts Phase 4 US2 tests (T048 .. T052) and Phase 5 US3 tests (T060 .. T070) while Dev A finishes US1.
3. After Phase 3 checkpoint: Dev A picks up US3 (Phase 5), Dev B picks up US2 (Phase 4) — they touch independent code paths post-Phase 3.
4. Dev A or B picks up US4 + Polish at the end.

---

## Notes

- `[P]` = different file or different function in the same file with no incomplete dependency.
- `[Story]` label maps every user-story task to its spec.md story (US1 / US2 / US3 / US4).
- US5 ("cancel silence") was dropped from v1; no tasks here. (Spec FR-022.)
- Tests are mandatory per Constitution III; every story phase begins with the failing-test wave.
- Commit boundary: typically after each green test or each completed task. Use Conventional Commits.
- After every task, run `pytest`, `mypy`, `ruff`, `black --check`, `lint-imports` — anything red blocks the next task.
- Avoid: vague task descriptions, same-file `[P]` collisions (see file-conflict map above), cross-story coupling that breaks story independence.
