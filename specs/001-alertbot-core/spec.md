# Feature Specification: AlertBot Core (FlashDuty → Lark Card → Alertmanager Silence)

**Feature Branch**: `001-alertbot-core`
**Created**: 2026-05-07
**Last Updated**: 2026-05-07 (clarifications resolved: FR-022 dropped, FR-023 + FR-024 decided)
**Status**: Ready for Plan
**Constitution**: AlertBot Constitution v1.0.0 (`.specify/memory/constitution.md`)
**Input**: User description: "AlertBot 是一个 Lark 应用机器人，串起 Alertmanager → FlashDuty → AlertBot → Lark
告警链路。FlashDuty 触发告警 → AlertBot 在指定 Lark 群发出互动卡片（带 severity 颜色 / 服务+时间+摘要 /
自动 @ 当前值班人 / 6 个静默按钮 5min/30min/1h/4h/24h/自定义）→ 点击静默按钮 → AlertBot 调用 Alertmanager
/api/v2/silences → 卡片原地更新为已静默 → FlashDuty incident.closed → 卡片再次更新为已恢复。纯规则驱动，
无 LLM/NLU/日志查询/K8s 操作/统计报表。"

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Alerts arrive as styled Lark cards (Priority: P1) 🎯 MVP

When a production alert fires in Alertmanager and reaches FlashDuty, the on-call engineer
sees an interactive card appear in the team's designated Lark group **within seconds**,
without leaving Lark. The card shows a severity-coloured title bar, the affected service,
the alert time, and a human-readable summary. When FlashDuty later marks the incident
closed, **the same card** updates in place to a green "resolved" state — no duplicate
cards, no stale "still firing" state in the chat history.

**Why this priority**: This is the visibility layer. The team already lives in Lark; just
getting alerts to appear there (with auto-resolve) is itself the dominant share of MTTA
reduction, and it can ship before any silencing logic exists. Without this, nothing else
matters.

**Independent Test**: Send a test FlashDuty `incident.created` webhook → verify a card
appears in the Lark group within 5 seconds with the correct severity colour, service,
time, and summary. Send a test `incident.closed` webhook for the same incident →
verify the **same card** (same `message_id`) updates to "resolved" state. No second card
is sent.

**Acceptance Scenarios**:

1. **Given** a configured Lark group and a registered FlashDuty webhook, **When**
   FlashDuty posts an `incident.created` event for a service of severity `critical`,
   **Then** within 5 seconds an interactive card appears in the group whose title bar is
   the configured "critical" colour, whose body lists the service name, the incident
   creation time (in the team's timezone), and the alert summary.
2. **Given** a card already exists for incident `INC-123` in `firing` state, **When**
   FlashDuty posts an `incident.closed` event for `INC-123`, **Then** the same Lark card
   (identified by its `message_id`) is updated in place to a green `resolved` state and
   no new card is posted.
3. **Given** FlashDuty redelivers an already-processed `incident.created` event for
   `INC-123` (same `incident_fingerprint`), **When** AlertBot receives the duplicate,
   **Then** no second card is posted, no duplicate database row is created, and the
   webhook responds successfully (replay-safe per Constitution principle II).
4. **Given** Lark sends a `type=url_verification` challenge to the event-callback URL,
   **When** AlertBot receives it, **Then** AlertBot returns `{"challenge": <value>}`
   within 5 seconds before any other processing (Constitution principle VIII).
5. **Given** a FlashDuty webhook arrives with an invalid signature, **When** AlertBot
   processes it, **Then** AlertBot returns HTTP 401 immediately and no card, database
   row, or audit entry referencing the payload's claimed incident is created.

---

### User Story 2 — Cards auto-mention the correct on-call engineer (Priority: P1)

When the alert card appears, the **currently on-call engineer for that service is
@-mentioned** in the card body. The engineer's phone buzzes; everyone else in the group
sees who owns the response. There is no longer a "群里喊一声看谁能处理" gap.

The on-call engineer is resolved using the four-tier priority chain ("D-plan"):
1. An explicit `lark_user` label on the FlashDuty incident, if present.
2. Otherwise, the FlashDuty schedule API for that service.
3. Otherwise, a static `service → user` mapping from configuration.
4. Otherwise, a configured fallback group role mention (e.g. the channel's `@on-call`).

**Why this priority**: This is what cuts MTTA from "minutes of human-routing" to "seconds
of phone-buzz." It depends on US1's card existing, but it is independently demoable: ship
US1 with a static "always @ the channel" rule, then layer US2 on top.

**Independent Test**: For each tier of the chain, configure exactly that tier (and
nothing higher in priority) → fire a test alert → verify the card @-mentions the user
that tier resolves to. For the fallback tier, verify the group role is mentioned, not
a personal user.

**Acceptance Scenarios**:

1. **Given** a FlashDuty incident whose `labels.lark_user` = `alice@company.com`, **When**
   AlertBot renders the card, **Then** `@alice` appears in the card body and no API call
   is made to the FlashDuty schedule endpoint or to the static map.
2. **Given** an incident with no `lark_user` label and a service `payment-api` whose
   FlashDuty schedule currently has `bob` on-call, **When** AlertBot renders the card,
   **Then** `@bob` appears in the card body.
3. **Given** the FlashDuty schedule API is unavailable, **When** AlertBot renders the
   card, **Then** AlertBot falls through to the static `service → user` mapping from
   configuration; if that also has no entry, the configured fallback group role is
   mentioned, and the failure of the schedule API is reported to the meta-channel
   (Constitution principle VI).
4. **Given** the on-call schedule was read in the last 5 minutes, **When** a second alert
   arrives for the same service, **Then** AlertBot uses the cached schedule (no second
   API call) — and the cache TTL is the only permitted form of polling (Constitution
   principle I exception).

---

### User Story 3 — One-click silence with fixed durations and real-operator attribution (Priority: P2)

The on-call engineer recognises the alert as a known false positive (or a deploy in
progress). They tap a single button on the card — `5min`, `30min`, `1h`, `4h`, or `24h`.
AlertBot creates a corresponding silence in Alertmanager and the **same card** updates
in place to a grey "silenced" state showing the expiry time and the operator's name.

Crucially, the silence in Alertmanager records the **real engineer's email** in its
`createdBy` field — not the bot identity. Any team member opening the Alertmanager UI
later can see exactly who silenced the alert, without correlating logs.

**Why this priority**: This is the headline value-prop (10× faster than hand-writing
matchers in the Alertmanager UI), but it depends on US1's card and US2's @-mention being
in place to be useful. Without US1, there is no card to put buttons on.

**Independent Test**: From a card in `firing` state, click `[30min]` → verify
(a) Alertmanager exposes a new silence with matchers derived from the alert's labels and
expiry ≈ now + 30 minutes; (b) the silence's `createdBy` equals the clicker's real email
(resolved from their Lark `user_id`); (c) the same card updates to `silenced` state with
the expiry timestamp and the operator's name visible; (d) an audit row is persisted
recording the click.

**Acceptance Scenarios**:

1. **Given** a card in `firing` state for incident `INC-123`, **When** a Lark user
   `alice` (whose Lark profile email is `alice@company.com`) taps `[30min]`, **Then**
   within 3 seconds Alertmanager has a new silence whose matchers correspond to the
   alert's identifying labels, whose expiry is ≈ now + 30 min, and whose `createdBy`
   is exactly `alice@company.com`.
2. **Given** the silence call to Alertmanager succeeds, **When** AlertBot updates the
   card, **Then** the same card (same `message_id`) is patched in place to a grey
   `silenced` state showing the expiry time (in the team's timezone) and "Silenced by
   Alice." No new card is posted.
3. **Given** AlertBot cannot reach Alertmanager within the configured timeout, **When**
   the engineer taps a silence button, **Then** the card surfaces a user-friendly failure
   notice ("Silence failed — Alertmanager unreachable. Please retry or use the AM UI."),
   the failure is reported to the meta-channel with `trace_id`, and no fake "silenced"
   state is shown on the card.
4. **Given** a duplicate Lark `card.action.trigger` callback for the same `event_id`,
   **When** AlertBot processes it, **Then** no second silence is created in Alertmanager
   (idempotency per Constitution principle II) and the engineer sees no duplicate state
   change.
5. **Given** a Lark `card.action.trigger` callback arrives with an invalid signature,
   **When** AlertBot processes it, **Then** AlertBot returns HTTP 401 and no silence is
   created.

---

### User Story 4 — Custom silence duration via Lark form modal (Priority: P3)

Sometimes 24 hours is too short and 24 hours plus is forbidden, but the engineer needs
something between the fixed buttons (e.g. "until end of release window, ~7 hours"). They
tap `[Custom]` on the card, a Lark form modal opens, they enter a duration, and the same
silence flow as US3 runs.

The custom duration MUST NOT exceed the system-wide hard cap of **24 hours**. If the
engineer enters more than 24 hours, the form is rejected with an inline message and the
engineer is guided to use the regular `24h` button or to re-silence after expiry.

**Why this priority**: A nice-to-have polish. The five fixed buttons cover ≥ 90 % of
real cases. This story can ship after the headline value (US3) is proven.

**Independent Test**: From a card in `firing` state, tap `[Custom]` → form modal opens
→ enter `7h` → submit → verify a silence is created with expiry ≈ now + 7 h and the
card updates as in US3. Repeat with input `25h` → verify the form is rejected with a
clear message and no silence is created.

**Acceptance Scenarios**:

1. **Given** a card in `firing` state, **When** the engineer taps `[Custom]` and submits
   `7h` in the resulting form, **Then** a silence is created with expiry ≈ now + 7 h and
   the card updates to `silenced` exactly as in US3.
2. **Given** a custom-duration form, **When** the engineer submits a duration > 24 h,
   **Then** the form is rejected with a message explaining the 24-hour cap, no silence
   is created, and the original card remains in `firing` state.
3. **Given** an invalid duration string (e.g. `"banana"`), **When** the engineer submits,
   **Then** the form is rejected with an inline parse-error message.

---

> **Note**: a former US5 ("Cancel a silence from the silenced card") was considered
> and **dropped from v1** during the clarification phase (2026-05-07). v1 has no
> in-card cancel-silence button; engineers cancel via the Alertmanager UI.

### Edge Cases

- **Lark `message_id` lost**: AlertBot persists the `(incident_fingerprint → lark_message_id)`
  mapping at card-creation time. If the database row is missing or unreadable when a
  state-update event arrives, AlertBot falls back to posting a new card prefixed with
  "[Original card lost]" and reports the inconsistency to the meta-channel. This is
  worse UX than in-place update but better than silently dropping the resolution event.
- **Alertmanager unreachable on silence**: see US3 scenario 3 — surface failure on the
  card; never fake a silenced state.
- **FlashDuty / Lark webhook redelivery**: deduplication keys are
  `incident_fingerprint` (FlashDuty) and `event_id` (Lark), enforced by a unique
  database constraint. Duplicate events are ack'd 200 OK without re-processing.
- **Severity change mid-flight**: if FlashDuty fires `incident.updated` with a new
  severity for an existing incident, the card's title-bar colour and summary update in
  place; the underlying silence (if any) is unaffected.
- **AlertBot service restart while a silence is in-flight**: in-flight HTTP work may be
  lost; the engineer's button click is, however, persisted as soon as the webhook is
  acknowledged, so on retry the system can replay or report the gap.
- **Alert resolves while card is in `silenced` state**: card transitions to `resolved`;
  the silence in Alertmanager is left to expire on its own (it was set with a duration,
  not "until manually cleared").
- **Operator's Lark profile has no email**: silence `createdBy` falls back to
  `lark:<user_id>` and the missing-email condition is reported to the meta-channel.
- **Configuration reload**: changes to the on-call mapping or button-set YAML take
  effect on the next event without a service restart (Constitution principle V).

---

## Requirements *(mandatory)*

> **Convention**: requirements are written from a user/business perspective. Technology
> bindings (Python, FastAPI, Pydantic, etc.) live in the Constitution and the plan, not
> here.

### Functional Requirements

#### Webhook ingestion

- **FR-001**: System MUST accept inbound `incident.created`, `incident.updated`, and
  `incident.closed` events from FlashDuty via HTTPS webhook and respond with HTTP 200
  within 2 seconds at p95.
- **FR-002**: System MUST cryptographically verify every inbound FlashDuty webhook
  signature before any business processing; verification failure MUST return HTTP 401
  immediately.
- **FR-003**: System MUST cryptographically verify every inbound Lark event-callback
  request (encrypted-payload mode supported); verification failure MUST return HTTP 401.
- **FR-004**: System MUST detect and correctly respond to Lark
  `type=url_verification` challenge requests with `{"challenge": <value>}` within 5
  seconds, and MUST do so before any signature-verification step that would reject the
  handshake body.
- **FR-005**: System MUST be idempotent on every inbound webhook: duplicate FlashDuty
  events (same `incident_fingerprint` + event type) and duplicate Lark events (same
  `event_id`) MUST NOT cause duplicate cards, duplicate silences, duplicate
  @-mentions, or duplicate audit entries; the deduplication MUST be enforced by a
  unique constraint at the database layer, not only in application logic.

#### Card lifecycle

- **FR-006**: On a new FlashDuty `incident.created`, the system MUST post a single
  interactive card to the configured Lark group whose title-bar colour is determined
  by the alert severity using a configured severity → colour map.
- **FR-007**: The card body MUST display the affected service, the incident creation
  timestamp in the team's configured timezone, and the alert summary text.
- **FR-008**: The card MUST present, when the alert is in `firing` state, six action
  buttons: `5min`, `30min`, `1h`, `4h`, `24h`, and `Custom`.
- **FR-009**: The system MUST persist the mapping `(incident_fingerprint →
  lark_message_id)` at card-creation time so that future state updates can locate the
  card.
- **FR-010**: All subsequent card-state changes for the same incident
  (`firing → silenced`, `silenced → resolved`, `firing → resolved`) MUST be applied as
  in-place updates to the same `message_id`. The system MUST NOT post a second card
  for the same incident under any circumstance other than the `message_id`-lost
  fallback path described in Edge Cases.
- **FR-011**: When the system enters the `message_id`-lost fallback path, it MUST post
  a new card prefixed `[Original card lost]` and MUST report the inconsistency to the
  meta-channel.

#### On-call resolution

- **FR-012**: The system MUST resolve the on-call engineer for an alert using the
  following priority chain, terminating at the first tier that produces a result:
  (1) `incident.labels.lark_user`, (2) FlashDuty schedule API, (3) static
  `service → user` mapping from configuration, (4) configured fallback group role.
- **FR-013**: The system MUST cache FlashDuty schedule reads with a TTL of at most
  5 minutes; this is the only form of pull-mode upstream access permitted (Constitution
  principle I exception).
- **FR-014**: The system MUST @-mention the resolved user in the card body when the
  card is in `firing` state.

#### Silence operations

- **FR-015**: On a fixed-duration silence button click, the system MUST translate the
  alert's identifying labels into Alertmanager silence matchers and create a silence
  via the Alertmanager `/api/v2/silences` endpoint with `endsAt = now + chosen
  duration` and `createdBy = the clicker's real email` (resolved via Lark
  `user_id → email` lookup).
- **FR-016**: On a `Custom`-duration submission, the system MUST validate the duration
  string and reject any value > 24 hours with a user-visible message; valid durations
  ≤ 24 h MUST follow the same flow as fixed-duration silences (FR-015).
- **FR-017**: The system MUST enforce a hard upper bound of 24 hours on every silence
  it creates, regardless of configured defaults; "unlimited" silences MUST NOT be
  reachable through any AlertBot path.
- **FR-018**: When the operator's Lark profile has no email, the system MUST fall back
  to `createdBy = lark:<user_id>` and MUST report the missing-email condition to the
  meta-channel.
- **FR-019**: On a successful silence creation, the system MUST update the card in
  place to a grey `silenced` state showing the expiry timestamp (team timezone) and
  the operator's display name.
- **FR-020**: On a silence-creation failure (timeout, 5xx, network), the system MUST
  surface a user-friendly failure notice on the card (without faking a `silenced`
  state), MUST report the failure to the meta-channel with `trace_id`, and MUST leave
  the card in its previous state.
- **FR-021**: On a FlashDuty `incident.closed` for an incident whose card exists, the
  system MUST update the card in place to a green `resolved` state. Any in-place
  Alertmanager silence is left to expire on its own.

#### Silence cancellation

- **FR-022**: **(RESOLVED — dropped from v1, 2026-05-07.)** v1 does NOT expose a
  `Cancel silence` button on the Lark card. Engineers cancel silences via the
  Alertmanager UI. AlertBot MUST NOT implement any in-card cancel-silence affordance,
  callback route, or `silenced → firing` state-machine transition driven by user
  action.

#### Authorization on silence buttons

- **FR-023**: **(RESOLVED — open to all group members, 2026-05-07.)** Any member of
  the Lark group MAY click any silence button. The system MUST NOT perform any
  authorization check (`is_oncall`, `is_admin`, FlashDuty role mapping, etc.) on
  silence-button callbacks. Accountability is enforced solely by recording the real
  operator's email in the Alertmanager silence's `createdBy` field (see FR-015) and
  in the audit log (see FR-025).

#### FlashDuty incident lifecycle alignment

- **FR-024**: **(RESOLVED — Alertmanager is the only silence target, 2026-05-07.)**
  When AlertBot creates an Alertmanager silence, it MUST NOT call any FlashDuty
  incident-acknowledge, incident-snooze, or incident-close API. Alertmanager is the
  single source of truth for silence state. FlashDuty's incident view may temporarily
  show an open incident while AM is silenced; this drift is accepted operational
  behaviour and is documented to the team. AlertBot's FlashDuty client is read-only
  for the schedule API and consumes incident-lifecycle webhooks; it MUST NOT make any
  state-mutating calls to FlashDuty.

#### Audit & observability

- **FR-025**: The system MUST persist an audit record for every inbound webhook
  receipt (FlashDuty + Lark), every outbound API call (Lark + FlashDuty +
  Alertmanager), every button click, and every silence creation/cancellation. Each
  record MUST capture **who** (Lark `user_id` and email when present), **when** (UTC
  timestamp), **what** (operation type + redacted payload), and **result**
  (success/failure + response summary). Sensitive values (tokens, secrets, private
  keys) MUST be redacted before persistence.
- **FR-026**: An audit-write failure MUST raise an alert to the meta-channel but MUST
  NOT block the main business flow.
- **FR-027**: Every outbound HTTP request from the system MUST set an explicit timeout
  (default 5 s) and use exponential-backoff retry up to a maximum of 3 attempts.
- **FR-028**: Every webhook-handling exception and every external-API failure MUST be
  reported to the operations meta-channel with `trace_id` and a redacted payload
  summary; silent error-swallowing is forbidden.

#### Configuration

- **FR-029**: The on-call priority chain, the static `service → user` mapping, the
  severity → card-colour map, the silence-duration button set, and the team timezone
  MUST live in YAML configuration validated by a schema. Configuration changes MUST
  take effect without a service restart and MUST NOT require a new release.

### Key Entities

- **Alert**: a single FlashDuty incident as observed by AlertBot. Identified by
  `incident_fingerprint`. Carries `service`, `severity`, `summary`, `labels`,
  `created_at`, `state` (`firing` / `silenced` / `resolved`), and the `lark_message_id`
  of the card representing it. Unique constraint on `incident_fingerprint`.
- **Silence**: a record of an Alertmanager silence created via AlertBot. Carries the
  `alertmanager_silence_id`, the `alert_fingerprint` it covers, `matchers`,
  `created_by` (real email or `lark:<user_id>` fallback), `starts_at`, `ends_at`, and
  `state` (`active` / `expired` / `cancelled`). Linked to its source button-click
  audit row.
- **Audit Entry**: a single recorded event. Carries `trace_id`, `timestamp_utc`,
  `actor_lark_user_id`, `actor_email_redacted`, `operation` (e.g.
  `webhook.flashduty.received`, `lark.card.update`, `alertmanager.silence.create`,
  `card.action.silence.click`), `payload_redacted`, `result` (`success` / `failure`),
  and `result_summary`.
- **Lark Card Message**: not persisted by us as content (Lark stores it); we persist
  only the `(incident_fingerprint, lark_message_id, state)` tuple, which is what makes
  in-place updates possible.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: From the moment FlashDuty triggers an alert, the corresponding card is
  visible in the Lark group within **5 seconds** at p95.
- **SC-002**: From the moment an engineer taps a silence button, the corresponding
  silence is observable in Alertmanager within **3 seconds** at p95.
- **SC-003**: For any single incident, redelivering its `incident.created` webhook
  100 times produces exactly **one** `Alert` row in the database and exactly **one**
  card in the Lark group (idempotency verified end-to-end).
- **SC-004**: 100 % of silences created by AlertBot show a real engineer's email (or
  the documented `lark:<user_id>` fallback) in Alertmanager's `createdBy` field;
  zero silences show "alertbot" or any bot identity.
- **SC-005**: For each known-noise alert resolved by silencing, median engineer
  handling time drops from the current baseline of **2–5 minutes** (silencing via the
  Alertmanager UI) to **≤ 10 seconds** end-to-end (tap → card shows `silenced`).
- **SC-006**: Every webhook receipt, every button click, every outbound API call, and
  every silence operation is queryable from the audit table by `trace_id`, with
  **100 %** coverage on a sample of 1 000 production events.
- **SC-007**: Operations meta-channel receives a notification within **30 seconds** of
  any unhandled exception or upstream-API failure, with `trace_id` for correlation.
- **SC-008**: No silence created by AlertBot exceeds 24 hours; verified by a periodic
  audit query (zero violations).
- **SC-009**: A configuration change (e.g. updating the static `service → user` map
  or the severity-colour table) takes effect on the next inbound alert with **zero
  service restarts**.
- **SC-010**: When an alert resolves, the original card transitions to `resolved`
  state in place; **zero** duplicate "resolved" cards are posted to the channel
  (verified by sampling 100 resolution events: each has exactly one card with three
  successive in-place state updates at most).

---

## Assumptions

- **Lark application form**: AlertBot is registered as an enterprise self-built Lark
  application (not a custom-webhook bot), so it can hold OAuth credentials and use the
  Open Platform card / event APIs.
- **Custom-duration UI**: when the engineer taps `[Custom]`, AlertBot opens a Lark
  **form modal** (recommended path from brainstorming O2). Alternative paths
  (secondary card / external web page) are explicitly rejected as more complex with
  no measurable benefit.
- **24-hour cap exceeded**: when a custom-duration submission exceeds 24 hours, the
  form is **rejected with an inline message guiding the engineer to use `[24h]` and
  re-silence after expiry**. Auto-clamping to 24 h is rejected (silently shortening
  the engineer's intent is worse than rejecting it). (Brainstorming O3.)
- **FlashDuty signature scheme**: the exact signature header / algorithm used by
  FlashDuty for outbound webhooks is an implementation detail to be confirmed against
  FlashDuty's official documentation during plan-phase research; this is **not** a
  scope decision and so is not raised as a NEEDS CLARIFICATION here. (Brainstorming
  O4.)
- **Public reachability**: the AlertBot service is exposed to the public internet via
  the company's Ingress / API gateway, because Lark and FlashDuty webhook-callback
  delivery require it.
- **Alertmanager service-account scope**: the Alertmanager API credentials given to
  AlertBot are scoped to silence creation, listing, and expiry only — no read access
  to alert routing config, no rule modification.
- **Single Lark group per AlertBot deployment**: v1 targets one team, one Lark group,
  one Alertmanager. Multi-tenant routing is explicitly out of scope (see Non-Goals).
- **FlashDuty handles upstream alert grouping/dedup**: AlertBot does not
  group/dedup/compress alerts on its own; it processes whatever incidents FlashDuty
  emits.
- **Team timezone is configurable**: a single team timezone is configured globally in
  YAML; all card timestamps render in that timezone.
- **Constitution-imposed tech stack**: Python 3.11+, FastAPI, HTTPX async, SQLAlchemy
  2 async, Pydantic v2, PostgreSQL (prod) / SQLite (local + tests), Vault / Sealed
  Secret for secrets, Docker + K8s + Helm for deployment. These are **fixed by the
  Constitution** and are not re-negotiated per feature.

---

## Out of Scope (Non-Goals)

The following are explicitly excluded from this feature and from the AlertBot product
in general; they belong to FlashDuty, to other tools, or to separate future projects:

- Natural-language conversation, LLM, NLU, agent capabilities of any kind.
- Log queries against Loki / ELK / any log backend.
- Kubernetes pod / deployment operations (restart, scale, delete, exec, etc.).
- Alert statistics, reports, or dashboards (use FlashDuty's built-in pages).
- Adapters for Slack / WeCom (企业微信) / Discord / other IM platforms.
- Multi-tenant or cross-team isolation; v1 is single-team / single-channel.
- Alert grouping, deduplication, or compression — FlashDuty handles these upstream.
- MCP protocol, LangChain, LangGraph, vector databases, Redis (Redis is permitted
  *only* if a measured deduplication-performance bottleneck appears, per Constitution).
- Active polling of FlashDuty / Lark / Alertmanager. Webhook push is the only inbound
  path; the on-call schedule cache (TTL ≤ 5 min) is the sole exception.

---

## Dependencies

- **Alertmanager**: must expose `/api/v2/silences` with a least-privilege service
  account scoped to silence operations.
- **FlashDuty**: must be configured to deliver `incident.created`,
  `incident.updated`, and `incident.closed` webhooks to AlertBot, signed.
- **Lark Open Platform**: AlertBot is registered as an enterprise self-built
  application with `im:message` (post / patch interactive cards) and event-callback
  permissions.
- **PostgreSQL** (production) / **SQLite** (local & tests): for the `Alert`,
  `Silence`, and `Audit` tables.
- **Vault or Sealed Secret**: for injecting Lark Encrypt Key / Verification Token /
  app credentials, the FlashDuty webhook signing secret, and the Alertmanager service
  account.
- **Public ingress / company gateway**: required for inbound webhook reachability.
