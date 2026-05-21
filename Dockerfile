# syntax=docker/dockerfile:1.7

# ───── builder ─────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=2.4.0 \
    POETRY_VIRTUALENVS_CREATE=true \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1 \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring

WORKDIR /build

RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential curl \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

# Copy dependency metadata first to maximize Docker layer cache reuse.
COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only=main

# Copy source and install the application package itself.
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN poetry install --only-root


# ───── runtime ─────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    ALERTBOT_CONFIG="/app/config/config.yaml"

RUN groupadd --system alertbot \
 && useradd --system --gid alertbot --no-create-home --shell /sbin/nologin alertbot

WORKDIR /app

COPY --from=builder --chown=alertbot:alertbot /build/.venv /app/.venv
COPY --from=builder --chown=alertbot:alertbot /build/app /app/app
COPY --from=builder --chown=alertbot:alertbot /build/migrations /app/migrations
COPY --from=builder --chown=alertbot:alertbot /build/alembic.ini /app/alembic.ini
COPY --chown=alertbot:alertbot config/example.yaml /app/config/config.yaml

USER alertbot
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
