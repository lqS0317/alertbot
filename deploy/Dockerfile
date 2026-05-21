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

# 先单独复制 lock + pyproject 利用 layer 缓存
COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only=main

# 再复制源码并安装项目本体
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./


# ───── runtime ─────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

# 非 root 用户
RUN groupadd --system alertbot \
 && useradd --system --gid alertbot --no-create-home --shell /sbin/nologin alertbot

WORKDIR /app
COPY --from=builder --chown=alertbot:alertbot /build/.venv /app/.venv
COPY --from=builder --chown=alertbot:alertbot /build/app /app/app
COPY --from=builder --chown=alertbot:alertbot /build/migrations /app/migrations
COPY --from=builder --chown=alertbot:alertbot /build/alembic.ini /app/alembic.ini
COPY --chown=alertbot:alertbot config/example.yaml /app/config/example.yaml

USER alertbot
EXPOSE 8000

# Liveness/readiness 走 /healthz；K8s probe 由 Helm chart 配置。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
