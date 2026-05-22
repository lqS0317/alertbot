# Quickstart — AlertBot Core (local development)

**Date**: 2026-05-07 · **Spec**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md)

This quickstart shows how to run AlertBot **locally** against PostgreSQL in Docker, with a public
tunnel for Lark + FlashDuty webhook delivery, and how to drive the three end-to-end
flows (FIRING / SILENCED / RESOLVED) by hand.

The full prod path (K8s + Helm + PostgreSQL) is in [`deploy/helm/alertbot/`](../../deploy/helm/alertbot/)
once T8 / T23 land — that is **NOT** what this file covers.

## 0. Prerequisites

- Python 3.11+ (`pyenv install 3.11.x` recommended)
- `poetry` 1.8+
- A tunnelling tool that gives a public HTTPS URL (`ngrok http 8000`, `cloudflared
  tunnel`, or the company's dev-tunnel). Lark and FlashDuty webhook callbacks
  require a public reachable URL.
- A Lark **enterprise self-built application** with:
  - `im:message` permission
  - Event-callback URL set to `<tunnel-url>/webhook/lark`
  - Encrypt Key + Verification Token noted (for local config)
- A FlashDuty workspace with a webhook integration pointing at
  `<tunnel-url>/webhook/fd` (signing secret noted)
- Docker Compose for local PostgreSQL
- An Alertmanager reachable from your laptop (local docker or company staging AM)
  with a service-account token scoped to silence operations

## 1. Clone & install

```bash
git clone <repo>
cd alertbot
poetry install
```

## 2. Configure

Copy and fill `config/example.yaml` to `config/local.yaml`:

```yaml
# config/local.yaml — LOCAL ONLY. Do NOT commit.
lark:
  app_id: "cli_abc..."
  app_secret_env: "LARK_APP_SECRET"          # we read from env, never from file
  encrypt_key_env: "LARK_ENCRYPT_KEY"
  verification_token_env: "LARK_VERIFY_TOKEN"
  group_chat_id: "oc_test_group..."
  meta_channel_id: "oc_meta_channel..."

flashduty:
  webhook_secret_env: "FD_WEBHOOK_SECRET"
  schedule_api_base: "https://api.flashcat.cloud/api/v1"
  schedule_api_token_env: "FD_API_TOKEN"

alertmanager:
  base_url: "http://localhost:9093"
  service_account_token_env: "AM_TOKEN"
  request_timeout_seconds: 5

oncall:
  priority_chain: [incident_label, fd_schedule, static_map, fallback_role]
  incident_label_key: "lark_user"
  static_service_map:
    payment-api: ["alice@company.com", "bob@company.com"]
    auth-svc:    ["carol@company.com"]
  fallback_role: ["@on-call"]
  schedule_cache_ttl_seconds: 300

severity_colors:
  critical: red
  warning:  orange
  info:     blue

silence_buttons:
  fixed_durations: [5min, 30min, 1h, 4h, 24h]
  enable_custom: true

timezone: "Asia/Shanghai"
max_silence_hours: 24
```

Export the secrets to your shell (do not bake them into the YAML):

```bash
export LARK_APP_SECRET=…
export LARK_ENCRYPT_KEY=…
export LARK_VERIFY_TOKEN=…
export FD_WEBHOOK_SECRET=…
export FD_API_TOKEN=…
export AM_TOKEN=…
export ALERTBOT_CONFIG=$PWD/config/local.yaml
export DATABASE_URL="postgresql+asyncpg://alertbot:alertbot@localhost:5432/alertbot"
```

## 3. Start PostgreSQL and run migrations

```bash
docker compose up -d postgres
poetry run alembic upgrade head
```

This creates the `alerts`, `silences`, and `audit_log` tables in the local PostgreSQL database
with all UNIQUE / CHECK constraints from [data-model.md](./data-model.md).

## 4. Start the service

```bash
poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Expose it publicly:

```bash
ngrok http 8000
# → https://abc123.ngrok.io
```

Update the Lark and FlashDuty webhook URLs to the ngrok host.

## 5. Smoke tests (the three end-to-end flows)

### 5.1 Lark `url_verification` handshake (Constitution VIII / FR-004)

Lark's admin console fires this automatically when you set the callback URL. To
trigger it manually:

```bash
curl -sX POST https://abc123.ngrok.io/webhook/lark \
  -H 'Content-Type: application/json' \
  -d '{"type":"url_verification","challenge":"hello-world","token":"$LARK_VERIFY_TOKEN"}'
# → {"challenge":"hello-world"}    (within 5 s; SC-001 sub-target)
```

### 5.2 FIRING flow (US1)

Trigger a synthetic FlashDuty `incident.created` event using `scripts/fd-fire.py`:

```bash
poetry run python scripts/fd-fire.py \
  --service payment-api \
  --severity critical \
  --summary "Synthetic alert from quickstart" \
  --target https://abc123.ngrok.io/webhook/fd
```

**Expected**:
- A red-titled interactive card appears in the configured Lark group within 5 s
  (SC-001).
- The card body shows `payment-api`, the alert time in `Asia/Shanghai`, and the
  summary.
- `@alice` is mentioned (per the static_service_map).
- Six buttons: `5min`, `30min`, `1h`, `4h`, `24h`, `Custom`.

Verify in DB:

```bash
docker compose exec postgres psql -U alertbot -d alertbot \
  -c "SELECT incident_fingerprint, state, lark_message_id FROM alerts;"
docker compose exec postgres psql -U alertbot -d alertbot \
  -c "SELECT operation, result FROM audit_log ORDER BY id DESC LIMIT 5;"
```

### 5.3 Idempotency check (SC-003)

Re-fire the same payload 100 times:

```bash
for i in $(seq 1 100); do
  poetry run python scripts/fd-fire.py --replay-fingerprint "alertname=HighCPU,instance=web-01"
done
docker compose exec postgres psql -U alertbot -d alertbot \
  -c "SELECT COUNT(*) FROM alerts WHERE incident_fingerprint='alertname=HighCPU,instance=web-01';"
# → 1
```

The Lark group must show **exactly one** card.

### 5.4 SILENCED flow (US3)

In the Lark group, click `[30min]` on the card.

**Expected**:
- Within 3 s (SC-002), Alertmanager has a new silence:
  ```bash
  curl -s http://localhost:9093/api/v2/silences | jq '.[] | {id, status, createdBy, endsAt, matchers}'
  ```
  with `createdBy = <your real Lark email>` (SC-004), `endsAt ≈ now + 30 min`,
  and matchers derived from the alert's labels.
- The same Lark card patches in place to a grey `Silenced by <Your Name>` state
  showing the expiry time. **No** second card is posted (SC-010).

Verify in DB:

```bash
docker compose exec postgres psql -U alertbot -d alertbot \
  -c "SELECT alertmanager_silence_id, created_by, duration_choice, state FROM silences;"
```

### 5.5 RESOLVED flow (US1, completion)

Trigger an `incident.closed` event for the same fingerprint:

```bash
poetry run python scripts/fd-fire.py \
  --event-type incident.closed \
  --fingerprint "alertname=HighCPU,instance=web-01" \
  --target https://abc123.ngrok.io/webhook/fd
```

**Expected**:
- The same Lark card patches in place to a green `Resolved` state.
- The Alertmanager silence is left in place (per FR-024) until its `endsAt` passes.

### 5.6 Custom duration (US4)

Tap `[Custom]` on a firing card → enter `7h` → submit → silence created with
`endsAt ≈ now + 7 h`.

Try again with `25h` → form rejected with the cap message; no silence created
(FR-016 / FR-017).

### 5.7 Hot-reload (SC-009)

While the service is running, edit `config/local.yaml` and change
`oncall.static_service_map.payment-api` to a different email. Save. Within 2 s
the file watcher picks it up. Fire a new alert: the new email is @-mentioned with
**no service restart**.

Verify the meta-channel was NOT pinged (no validation error). Now break the YAML
deliberately (e.g. an unindented line). Save. The meta-channel receives a
"config-reload validation failed" message and the previous good snapshot is
retained — the next alert still uses the old (good) config.

## 6. Test suite

```bash
poetry run pytest                      # unit + integration
poetry run pytest --cov=app --cov-report=term-missing
poetry run mypy app
poetry run ruff check app tests
poetry run black --check app tests
poetry run lint-imports                # import-linter
```

CI runs all of the above as gating checks (T0.d).

## 7. What's NOT in this quickstart

- Multi-replica behaviour (v1 runs as one Pod; the dedup contract is still
  DB-level for future scale-out).
- The `helm rollback` recipe for production — see `docs/rollback.md` (T24).
- The on-call runbook for AlertBot itself — see `docs/runbook.md` (T24).
