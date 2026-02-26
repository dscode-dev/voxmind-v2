FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends     ca-certificates     && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir poetry==1.8.3

COPY pyproject.toml README.md /app/
RUN poetry config virtualenvs.create false  && poetry install --no-interaction --no-ansi --only main

COPY voxmind /app/voxmind
COPY prompts /app/prompts

CMD ["python", "-m", "voxmind.main"]
