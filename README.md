# AlertBot

AlertBot is a rule-driven Lark application bot for the internal Alertmanager / FlashDuty
→ AlertBot → Lark alerting flow. It receives Alertmanager webhook v4 payloads and
FlashDuty incident webhooks, posts Lark interactive cards, @-mentions resolved on-call
engineers, and creates Alertmanager silences from card actions.

Authoritative project documents:

- Constitution: `.specify/memory/constitution.md`
- Feature spec: `specs/001-alertbot-core/spec.md`
- Implementation plan: `specs/001-alertbot-core/plan.md`
- Tasks: `specs/001-alertbot-core/tasks.md`
- Quickstart: `specs/001-alertbot-core/quickstart.md`

## Hard Boundaries

- No LLM, NLU, agent framework, MCP, vector database, Redis, Celery, or message queue.
- No Slack / WeCom / Discord adapter in v1.
- No FlashDuty incident ack/close/snooze writes; FlashDuty is read-only for schedules.
- Alertmanager `/api/v2/silences` is the silence source of truth.

## Runtime Behavior

### Alert Lifecycle

- **Firing**: posts one Lark interactive card per Alertmanager alert. The card includes
  cluster, environment, service, severity, trigger time, alert target, description,
  runbook link, monitor link, on-call assignee, and a silence duration dropdown.
- **Silenced**: card action callbacks create an Alertmanager `/api/v2/silences` entry,
  then patch the original card to a grey `SILENCED` state while keeping the full alert
  context. The callback returns a quick Lark toast first and performs the slow silence
  creation work in a background task to avoid Lark `200341` callback timeouts.
- **Resolved**: patches the original card to a green `RESOLVED` state and also posts a
  new resolved card to the same routed chat, so recovery is visible at the bottom of the
  Lark conversation.

Alertmanager webhook payloads containing multiple alerts are processed alert-by-alert.
Each alert has independent audit/idempotency using `<fingerprint>:<status>`.

### Card Field Mapping

- `cluster`: `labels.cluster`
- `env`: `labels.env`
- `service`: `labels.service` → `labels.job` → `labels.app` → `labels.component` →
  extracted from `annotations.description` lines like `服务: "..."` → `labels.alertname`
- `target`: `labels.instance` → `labels.pod` → `labels.node` → `labels.host` →
  `labels.target`
- `description`: `annotations.description` → `annotations.summary` → incident summary
- `runbook`: `annotations.runbook_url` → `cards.links.runbook_default_url`
- `monitor link`: `generatorURL`, optionally rewritten by
  `cards.links.generator_url_rewrites`

### Lark Callback Compatibility

Lark has multiple card callback/signature formats in the wild. AlertBot accepts both:

- v2 encrypted callbacks: SHA256 signature, `Encrypt Key`, encrypted body.
- legacy/plain callbacks: SHA1 signature, `Verification Token`, plain body.

For production, configure both `LARK_ENCRYPT_KEY` and `LARK_VERIFY_TOKEN`. Prefer a long
random Encrypt Key, not a short dictionary word. The request URL for both Lark event and
card callback configuration is:

```text
https://<alertbot-domain>/webhook/lark
```

Subscribe to the `card.action.trigger` callback in Lark Developer Console.

### On-call Resolution

On-call resolution follows the configured priority chain:

```yaml
oncall:
  priority_chain: [incident_label, fd_schedule, static_map, fallback_role]
```

- `incident_label`: reads the configured alert label, usually `lark_user`.
- `fd_schedule`: reads FlashDuty schedule API, with a short in-process cache.
- `static_map`: maps service name to email list.
- `fallback_role`: supports mixed email and role text. Email-like entries are resolved
  through Lark user lookup and rendered as real mentions when possible. Lookup failures
  degrade to plain text email to avoid Lark rejecting the entire card for one typo.

### Lark Chat Routing

AlertBot can route alerts to different Lark chats by alert labels:

```yaml
lark:
  group_chat_id: "oc_default"
  routes:
    - match: {cluster: "hsk-ops-infra"}
      chat_id: "oc_ops_alerts"
    - match: {env: "prod", severity: "critical"}
      chat_id: "oc_prod_critical"
```

Routes are evaluated in order. The first route whose `match` entries all exactly match
`alert.labels` wins. If none match, `group_chat_id` is used.

### Alertmanager / VMAlertmanager Receiver

To receive Alertmanager alerts, point a webhook receiver at `/webhook/am` and enable
resolved notifications:

```yaml
receivers:
  - name: alertbot
    webhook_configs:
      - url: http://alertbot.ops.svc.cluster.local:8000/webhook/am
        send_resolved: true
        http_config:
          bearer_token_secret:
            name: alertbot-am-webhook-token
            key: token
```

The bearer token must match `ALERTBOT_AM_WEBHOOK_TOKEN` in the AlertBot pod.

During migration from existing receivers, add an AlertBot route with `continue: true` so
legacy notification paths keep working while AlertBot is verified.

## Local Verification

```bash
poetry run black --check app tests
poetry run ruff check app tests
poetry run mypy app
poetry run lint-imports
poetry run pytest tests/ --cov=app --cov-fail-under=80
```

## Runtime

```bash
docker compose up -d postgres
export ALERTBOT_CONFIG=$PWD/config/local.yaml
export DATABASE_URL="postgresql+asyncpg://alertbot:alertbot@localhost:5432/alertbot"
poetry run alembic upgrade head
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Core endpoints:

- `GET /healthz`
- `GET /metrics`
- `POST /webhook/fd`
- `POST /webhook/am`
- `POST /webhook/lark`

Operational docs:

- `docs/runbook.md`
- `docs/rollback.md`
- `docs/operating/hot-reload.md`
- `docs/final-acceptance-review.md`
