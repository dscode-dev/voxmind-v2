FROM python:3.11-slim AS builder

ARG POETRY_INSTALL_GROUPS=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=2.0.1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR=/tmp/poetry-cache

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "poetry==$POETRY_VERSION"

WORKDIR /app

COPY pyproject.toml poetry.lock* /app/

RUN if [ -n "$POETRY_INSTALL_GROUPS" ]; then \
        poetry install --without dev --with "$POETRY_INSTALL_GROUPS" --no-root; \
    else \
        poetry install --only main --no-root; \
    fi \
    && rm -rf /tmp/poetry-cache

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 appuser

WORKDIR /app

COPY --from=builder /usr/local /usr/local
COPY app /app/app

RUN chown -R appuser:appuser /app

USER appuser

CMD ["python", "-m", "app.main"]
