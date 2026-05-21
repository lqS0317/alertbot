<!--
Sync Impact Report
==================
Version change: (template, unversioned) → 1.0.0
Bump rationale: Initial ratification. The previous file at .specify/memory/constitution.md
contained only the unfilled template (placeholder tokens). This is the first concrete,
project-specific constitution for AlertBot, hence MAJOR 1.0.0.

Modified principles (renamed/added vs. template placeholders):
  - [PRINCIPLE_1_NAME] → I. Webhook-First, Polling-Last
  - [PRINCIPLE_2_NAME] → II. Idempotent & Replay-Safe (NON-NEGOTIABLE)
  - [PRINCIPLE_3_NAME] → III. Test-First Development (NON-NEGOTIABLE)
  - [PRINCIPLE_4_NAME] → IV. Audit Everything
  - [PRINCIPLE_5_NAME] → V. Config-Driven, Not Hardcoded
  - (added)           → VI. Fail Fast & Visible
  - (added)           → VII. Verify Every Webhook (NON-NEGOTIABLE)
  - (added)           → VIII. Lark URL Verification First

Added sections (replacing the template's two generic [SECTION_*] slots, expanded to four
because the project requires them):
  - Tech Stack Constraints
  - Quality Standards
  - Security Requirements
  - Operational Constraints
  - Development Workflow

Removed sections: none (all template slots were placeholders; no prior content existed).

Templates requiring updates:
  - ✅ .specify/templates/plan-template.md — reviewed; existing "Constitution Check" gate
       and Technical Context fields remain compatible. No edits required: the gate is
       generic ("Gates determined based on constitution file") and will pick up the
       eight principles defined here at plan time.
  - ✅ .specify/templates/spec-template.md — reviewed; user-story / FR / SC structure
       does not conflict with any new principle. No edits required.
  - ✅ .specify/templates/tasks-template.md — reviewed; phased layout (Setup →
       Foundational → User Stories → Polish) is compatible with TDD-first, audit, and
       config-driven principles. No edits required.
  - ✅ .specify/templates/checklist-template.md — reviewed; generic, no edits required.
  - ⚠ .specify/templates/commands/*.md — directory not present in this workspace; no
       command-template files to reconcile. If added later, they MUST reference these
       eight principles by name rather than agent-specific identifiers.
  - ⚠ README.md / docs/quickstart.md — not present at repo root; create on first
       feature plan to point developers at this constitution.

Follow-up TODOs: none. All placeholder tokens have been replaced with concrete values.
-->

# AlertBot Constitution

AlertBot is an internal-team Lark application bot that receives FlashDuty alert webhooks,
renders them as **interactive Lark cards**, automatically @-mentions the current on-call
engineer, and provides one-click "silence" buttons that call the Alertmanager
`/api/v2/silences` API directly. It is purely rule-driven; it MUST NOT contain any
AI/LLM, natural-language, log-query, K8s-operations, or alert-statistics functionality
(those concerns belong to FlashDuty's built-in features or to separate downstream
projects).

## Core Principles

### I. Webhook-First, Polling-Last

All inbound external signals — FlashDuty alert events and Lark card-action callbacks —
MUST arrive via webhook push. Active polling of FlashDuty or Lark is forbidden.
**Sole exception**: on-call schedule data MAY be read via cached pull (TTL ≤ 5 minutes)
because schedules change slowly and webhook push is not offered for them; this read
path is not considered "polling" in the sense banned above.

Rationale: webhooks give us at-most-seconds latency, are explicitly supported by every
upstream system in our chain, and avoid the rate-limit and freshness traps of polling.

### II. Idempotent & Replay-Safe (NON-NEGOTIABLE)

Every webhook handler MUST be idempotent. FlashDuty and Lark both retry on timeouts and
both can re-deliver the same event. Re-processing a duplicate event MUST NOT produce a
duplicate Lark card, a duplicate Alertmanager silence, or a duplicate @-mention.
Deduplication MUST use stable event identifiers — `event_id` for Lark callbacks,
`incident_fingerprint` (or equivalent) for FlashDuty alerts — and MUST be enforced by a
unique constraint at the database layer, not only in application code.

Rationale: at-least-once delivery is an upstream contract we do not control; idempotency
is the only correct local response.

### III. Test-First Development (NON-NEGOTIABLE)

TDD is mandatory: write a failing test → implement just enough to make it pass → refactor.
The four core modules — webhook handlers, the Alertmanager client, the on-call resolver,
and the card renderer — MUST each have unit tests AND integration tests. Integration
tests MUST use `httpx` `MockTransport` to simulate the Lark, FlashDuty, and Alertmanager
HTTP surfaces; live network calls in tests are forbidden.

Rationale: this bot operates on alert paths that are themselves the canary for production
incidents. Bugs here amplify outages instead of mitigating them.

### IV. Audit Everything

Every inbound webhook receipt, every outbound API call (Lark / FlashDuty / Alertmanager),
and every button click MUST be persisted to an audit table. Audit records MUST capture:
**who** (Lark `user_id` and email when present), **when** (UTC timestamp), **what**
(operation type + redacted payload), and **result** (success/failure + response summary).
Failure to write an audit record MUST raise an alert to the meta-channel (see
Principle VI) but MUST NOT block the main business path.

Rationale: silencing alerts is a privileged operation against production observability.
Without an immutable audit trail we cannot answer "who silenced what, when, and why."

### V. Config-Driven, Not Hardcoded

The on-call resolution priority chain, Lark card templates, button options (silence
duration choices), and the `service → default on-call` mapping MUST live in YAML
configuration validated by Pydantic v2 schemas. Business code in Python MUST NOT
contain these values as literals. A configuration change MUST NOT require a new release.

Rationale: alerting policy changes faster than code; coupling them forces operations
work into the engineering release cycle and slows incident response.

### VI. Fail Fast & Visible

Any webhook-handling exception and any external API-call failure MUST be reported
explicitly to a dedicated operations meta-channel (a separate Lark group), including
the full `trace_id` and a redacted payload summary. Silent `try`/`except` swallowing
of errors is forbidden. Catching for translation into a typed error is permitted only
when the catch site re-raises or re-publishes through the meta-channel reporter.

Rationale: an alert bot that fails silently is worse than no alert bot — it gives
false confidence.

### VII. Verify Every Webhook (NON-NEGOTIABLE)

All inbound webhooks MUST be cryptographically verified before any business logic runs.
Lark callbacks MUST be validated using the Encrypt Key + Verification Token + timestamp
signature scheme; FlashDuty callbacks MUST be validated using FlashDuty's documented
signature mechanism. A verification failure MUST return HTTP `401` immediately and MUST
NOT touch downstream state, logs (beyond a redacted security event), or external APIs.

Rationale: this service is publicly reachable by design (see Operational Constraints).
Unverified webhooks are an arbitrary remote-trigger surface against production silences.

### VIII. Lark URL Verification First

Every Lark event-callback route MUST handle the `type=url_verification` challenge:
when Lark posts `{"type": "url_verification", "challenge": "<value>"}`, the route MUST
respond with `{"challenge": "<value>"}` within Lark's 5-second SLA, and MUST do so
before any signature-verification path that would reject the empty/handshake body.
This handshake is a hard prerequisite for registering the callback URL in the Lark
admin console.

Rationale: failing this handshake makes the bot un-installable; it has cost real days
in past attempts and so is hoisted to a first-class, named principle.

## Tech Stack Constraints

The following stack is fixed for AlertBot. Any deviation requires a constitution
amendment.

- Language: Python 3.11+
- HTTP framework: FastAPI
- HTTP client: HTTPX (async mode)
- ORM: SQLAlchemy 2.0 (async mode)
- Database: PostgreSQL (production) / SQLite (local + tests)
- Data validation: Pydantic v2
- Testing: pytest + pytest-asyncio + `httpx.MockTransport`
- Code style: black + ruff
- Type checking: mypy in strict mode
- Container: Docker (multi-stage build)
- Deployment: Kubernetes via Helm chart
- Secrets: HashiCorp Vault or Sealed Secret

The following are **explicitly disallowed** unless a constitution amendment introduces
them with documented justification: LangChain, LangGraph, vector databases, MCP,
Redis (permitted ONLY if a measured deduplication-performance problem appears), and
message queues.

## Quality Standards

- Type annotations: `mypy --strict` MUST pass on every commit.
- Code style: `black` and `ruff` MUST pass on every commit.
- Test coverage: the core modules — `webhooks`, `lark`, `alertmanager`, `oncall`, `cards`
  — MUST each maintain ≥ 80 % line coverage.
- Performance: webhook-handler p95 latency MUST be ≤ 2 seconds end-to-end. The Lark
  `url_verification` path specifically MUST be ≤ 5 seconds (Lark's hard cap).
- Card rendering: each alert MUST use a single Lark `message_id` for its initial card
  and all subsequent state updates (firing → silenced → resolved). Re-sending a new
  card on state change is forbidden — updates MUST be in-place edits.

## Security Requirements

- All secrets MUST be injected via Vault or Sealed Secret. Secrets MUST NOT appear in
  ConfigMaps, environment variables baked into images, source code, or log output.
- Lark event callbacks MUST support Lark's encrypted-payload mode (AES + Encrypt Key
  decryption).
- Alertmanager API calls MUST authenticate via a least-privilege service account scoped
  to silence creation/listing only.
- Audit logs MUST NOT contain full secrets, tokens, or private keys. Sensitive values
  MUST be redacted (e.g., last-4 fingerprint or hashed) before persistence.
- All outbound HTTP requests MUST set an explicit timeout (default 5 seconds) and MUST
  use exponential-backoff retry with a maximum of 3 attempts.

## Operational Constraints

- Alert path: Alertmanager → FlashDuty → AlertBot → Lark.
- Silence layer: Alertmanager `/api/v2/silences` is the default and only silence target.
  AlertBot MUST NOT touch FlashDuty's incident-silence surface.
- On-call resolution priority chain (the "D-plan"):
  1. `incident.label.lark_user` (explicit override on the incident itself)
  2. FlashDuty schedule API
  3. Static `service → default on-call` mapping (from YAML config)
  4. Fallback: the channel's default `on-call` role mention
- Silence-duration buttons are fixed: `5min` / `30min` / `1h` / `4h` / `24h` /
  `custom`.
- Card state machine: `firing` (red) → `silenced` (grey, with expiry time and operator
  identity) → `resolved` (green).
- The service MUST be reachable from the public internet (Lark and FlashDuty webhook
  callbacks require it), exposed via the Kubernetes Ingress and the company gateway.

## Development Workflow

- Commit messages MUST follow Conventional Commits (`feat` / `fix` / `chore` /
  `refactor` / `test` / `docs`).
- Code comments are written in Chinese.
- All specs, plans, tasks, and checklists live under `.specify/` (this repository's
  Spec Kit root).
- A pull request MUST receive at least one review approval AND MUST have green CI
  (lint + typecheck + test + coverage gate) before it can be merged.
- Deployment promotion path: `local` (SQLite) → `staging` (Kubernetes + PostgreSQL) →
  `production` (Kubernetes + PostgreSQL). A change MUST be observed working in
  `staging` before promotion to `production`.

## Governance

This constitution supersedes any informal practice or ad-hoc convention. In any
conflict between this document and a code review, design doc, or local team norm, this
document wins until amended.

**Amendment procedure.** A change to this constitution requires:
1. A pull request that updates `.specify/memory/constitution.md` and bumps the version
   per the policy below.
2. Explicit reviewer sign-off from at least one project maintainer.
3. A Sync Impact Report (the HTML comment at the top of this file) describing
   propagation to `.specify/templates/*` and any dependent runtime docs.

**Versioning policy (semantic).**
- **MAJOR**: a backward-incompatible change to governance, or the removal or
  redefinition of an existing principle (e.g., dropping the "Webhook-First" rule).
- **MINOR**: a new principle or section is added, or guidance is materially expanded.
- **PATCH**: clarifications, wording fixes, typo corrections, non-semantic
  refinements.

**Compliance review.** Every PR MUST pass the "Constitution Check" gate in
`.specify/templates/plan-template.md`. Reviewers MUST verify that the change does not
silently violate any NON-NEGOTIABLE principle (II, III, VII). Any deliberate deviation
MUST be recorded in the plan's Complexity Tracking table with a justification and a
rejected simpler-alternative.

**Runtime guidance.** Day-to-day development guidance (style, tooling specifics,
debugging recipes) lives in this repository's `README.md` and any agent-specific files
(e.g., `AGENTS.md`, `.cursor/rules/`); those documents MUST defer to this constitution
when they conflict.

**Version**: 1.0.0 | **Ratified**: 2026-05-07 | **Last Amended**: 2026-05-07
