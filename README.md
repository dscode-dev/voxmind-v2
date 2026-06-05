# VoxMind / ClipFlow V2

Autonomous, observable AI content factory. Discovers trending videos per theme, generates
cuts, and publishes — with a frontend that acts as a live **Operations Center**.

> **Deployment: Docker Compose** (single main server). Kubernetes is no longer used; the
> `k8s/` directory is historical/deprecated. See `docs/ARCHITECTURE_V2.md` for the full
> architecture and the V2 evolution plan.

## Components

- **control-plane** — Telegram bot (`/new`, `/finalize`) that enqueues jobs onto a Redis queue.
- **worker** — GPU container that runs the pipeline (download → faster-whisper → diarize →
  chunk → candidates → prompt → LLM → cut/render). One video = one run.
- **clipflow-api** — FastAPI backend: auth, jobs, pipeline state, artifacts, and the realtime
  event hub (SSE).
- **clipflow-studio** — React/Vite dashboard + Operations Center.
- **redis** (queue + event pub/sub), **minio** (artifacts), **postgres** (state).

## Quick start

```bash
cp .env.compose.example .env   # edit secrets (Telegram, MinIO, Postgres, JWT, OpenAI...)
docker compose up -d --build
```

clipflow-api runs `alembic upgrade head` on boot. Default ports: studio `:3000`,
api `:8010`, control-plane `:8000`, MinIO `:9000/:9001`.

## Pipeline flows

- **Manual / paid (`ClipJob`)**: `/new <url>` → prepare → operator returns `response.json`
  (or uses `--build-ia` for the OpenAI path) → `/finalize` → cuts.
- **Autonomous (`PipelineJob`, V2)**: scheduler discovers a candidate → enqueues → worker
  runs the same pipeline → publish. Tracked by the `PipelineState` machine and streamed to the
  Ops Center.

## Development

```bash
# API
cd clipflow-api && poetry install && alembic upgrade head && pytest
# Worker
cd worker && poetry install && pytest
# Studio
cd clipflow-studio && npm install && npm run dev
```

See `docs/ARCHITECTURE_V2.md` for module map, state machine, and event model.
