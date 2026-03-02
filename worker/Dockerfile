FROM python:3.11-slim

# =========================
# System deps
# =========================
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        build-essential \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# =========================
# Non-root user
# =========================
RUN useradd -m appuser

# =========================
# Install Poetry 2.x
# =========================
ENV POETRY_VERSION=2.0.1
RUN pip install "poetry==$POETRY_VERSION"

WORKDIR /app

# =========================
# Copy dependency files
# =========================
COPY pyproject.toml poetry.lock* /app/

# =========================
# Poetry config
# =========================
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --only main --no-root

# =========================
# Copy source
# =========================
COPY app /app/app

RUN chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "app.main"]