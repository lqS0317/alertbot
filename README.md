# AlertBot

AlertBot is a rule-driven Lark application bot for the internal Alertmanager → FlashDuty
→ AlertBot → Lark alerting flow. It receives FlashDuty incident webhooks, posts Lark
interactive cards, @-mentions the resolved on-call engineer, and creates Alertmanager
silences from card actions.

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
- `POST /webhook/lark`

Operational docs:

- `docs/runbook.md`
- `docs/rollback.md`
- `docs/operating/hot-reload.md`
- `docs/final-acceptance-review.md`
