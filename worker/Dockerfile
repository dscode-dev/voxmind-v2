# =========================
# Base Image
# =========================
FROM python:3.11-slim AS base

# =========================
# System dependencies
# =========================
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        build-essential \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# =========================
# Create non-root user
# =========================
RUN useradd -m appuser

# =========================
# Install Poetry
# =========================
ENV POETRY_VERSION=1.7.1
RUN pip install "poetry==$POETRY_VERSION"

# =========================
# Workdir
# =========================
WORKDIR /app

# =========================
# Copy dependency files
# =========================
COPY pyproject.toml poetry.lock* /app/

# =========================
# Configure Poetry
# =========================
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --only main

# =========================
# Copy application code
# =========================
COPY app /app/app

# =========================
# Set permissions
# =========================
RUN chown -R appuser:appuser /app

USER appuser

# =========================
# Environment defaults
# =========================
ENV PYTHONUNBUFFERED=1

# =========================
# Entry point
# =========================
CMD ["python", "-m", "app.main"]