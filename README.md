# VoxMind V2 (MVP skeleton)

This is an **MVP V1 of the V2** architecture: routing `/new <url> --v2` through a new orchestrator with
LLM routing + cache hooks, ready to run locally **or** in Kubernetes.

## What is implemented (MVP)
- Telegram bot command: `/new <url> --v2`
- V2 orchestrator skeleton with:
  - transcript chunking stub
  - LLM Router (OpenAI-compatible Chat Completions)
  - Cost tracking (approx, placeholder)
  - Cache interface (Redis-backed; safe no-op if disabled)
- Production-friendly config via environment variables
- Dockerfile + K8s manifests (basic)

> NOTE: Download/ASR/render steps are **stubs** in this MVP. We’ll iteratively wire your real pipeline.

## Quickstart (local)

### 1) Install deps
```bash
poetry install
```

### 2) Create `.env`
```bash
cp .env.example .env
# Fill TELEGRAM_BOT_TOKEN and OPENAI_API_KEY (optional if using MOCK_LLM=1)
```

### 3) Run
```bash
poetry run python -m voxmind.main
```

## Telegram usage
- `/new https://youtube.com/watch?v=... --v2` runs V2 MVP flow

## Kubernetes
See `k8s/` for sample deployment + secret + redis (optional).
You should provide:
- TELEGRAM_BOT_TOKEN
- OPENAI_API_KEY (or set MOCK_LLM=1)

## Environment variables
See `.env.example`.

---
Next steps (we’ll do together):
1) Wire real downloader + ASR step (whisper/faster-whisper)
2) Candidate builder + scoring agent + hook agent
3) Render (9:16 framing + subtitles)
4) Add persistent job storage + metrics
