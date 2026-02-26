FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 10001 appuser
WORKDIR /app

COPY pyproject.toml poetry.lock* /app/

RUN pip install --no-cache-dir poetry \
 && poetry config virtualenvs.create false \
 && poetry install --no-interaction --no-ansi

COPY app /app/app

RUN mkdir -p /work && chown -R appuser:appuser /work
USER appuser

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
