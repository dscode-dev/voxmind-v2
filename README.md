# ClipFlow / VoxMind

AI content pipeline: it downloads a video, transcribes and analyses it, selects narrative
cuts with an LLM, renders them, and delivers the finished clips.

> **Deployment: Docker Compose**, single host. Kubernetes is no longer used.
> See `docs/ARCHITECTURE_V2.md` for the module map and the V2 evolution plan.

## Components

| Service | What it does |
|---|---|
| **control-plane** | Telegram bot (`/new`, `/finalize`) that enqueues jobs onto a Redis queue. Deny-by-default: only allowlisted chats/users can run commands. |
| **worker** | GPU container running the pipeline: download → faster-whisper → diarize → chunk → candidates → prompt → LLM → cut/render. One video = one run. |
| **clipflow-api** | FastAPI backend: auth, jobs, pipeline state, artifacts, and the realtime event hub (SSE). |
| **redis** | Job queue (`voxmind_jobs`) and event pub/sub (`clipflow:events`). |
| **minio** | Artifact storage. |
| **postgres** | Application state. |

### Frontend

The `clipflow-studio` frontend **is not part of this repository**. It has no git history
here and is listed in `.gitignore`. Its Compose service was removed in PR-BOOT-01 so that
`docker compose up` no longer fails on an image that nothing builds. It will be restored in
a dedicated PR. Until then the API is driven directly (`/docs`) and the operational surface
is Telegram.

## Requirements

- Docker Engine with Compose v2 (v2.20+ — the worker build uses `additional_contexts`)
- **NVIDIA Container Toolkit**, for the GPU worker
- ~30 GB free disk for images (the worker base carries CUDA, torch and preloaded ASR models)

No Python, Poetry or Node installation is needed on the host: everything builds in Docker.

## Quick start

```bash
git clone <repo> clipflow
cd clipflow
cp .env.compose.example .env
# Fill in every value marked REQUIRED in .env (see "Configuration" below)
docker compose up -d --build
```

That single command builds all four images — including the worker's two-stage build — and
starts the stack.

## Configuration

`.env.compose.example` documents every variable, grouped by area. These have **no defaults**
and the stack will not start without them:

| Variable | Notes |
|---|---|
| `POSTGRES_PASSWORD` | |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | |
| `JWT_SECRET` | Min 16 chars. No fallback secret exists; the API refuses to start without it. |
| `INTERNAL_API_TOKEN` | Min 16 chars. Shared secret for `/internal/*`. The API refuses to start without it, and internal calls are rejected when it is missing. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | |
| `OPENAI_API_KEY` | Required when `AI_MODE=automatic` (the default) and no local LLM is enabled. |

Generate secrets with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### MinIO: internal vs public endpoint

Two endpoints, deliberately separate:

- **internal** (`minio:9000`, set by Compose) — service-to-service traffic.
- **public** (`MINIO_PUBLIC_ENDPOINT`) — the address a browser can reach.

Presigned asset URLs are signed against the *public* endpoint. SigV4 covers the `Host`
header, so a URL signed against the internal hostname cannot simply be rewritten later — it
has to be signed against the host the client will actually call. Set
`MINIO_PUBLIC_ENDPOINT` to whatever users type (`localhost:9000` locally, your domain in
production).

### Telegram authorization

The bot is **deny-by-default**. A chat or user that is not allowlisted cannot create jobs,
finalize jobs, or submit JSON; the attempt is logged and nothing is enqueued.

- `TELEGRAM_ALLOWED_CHAT_IDS` — comma-separated chat ids
- `TELEGRAM_ALLOWED_USER_IDS` — comma-separated user ids

If both are empty, `TELEGRAM_CHAT_ID` is used as the single authorized chat. If that is
empty too, nothing is authorized.

### Login codes

There is **no SMS provider integrated yet**. In production the login code is generated
randomly and written only to the API log:

```bash
docker compose logs clipflow-api | grep otp_issued_without_sms_provider
```

For local development you can pin the code, but only with an explicit development
environment — setting `FIXED_TEST_OTP` while `ENVIRONMENT=production` makes the API refuse
to start:

```bash
ENVIRONMENT=development
FIXED_TEST_OTP=123456
```

## Build

| Image | Dockerfile | Role |
|---|---|---|
| `clipflow-api` | `clipflow-api/Dockerfile` | production |
| `clipflow-control-plane` | `control-plane/Dockerfile` | production |
| `clipflow-worker-base` | `worker/Dockerfile.gpu.base` | production — CUDA 12.4 runtime, cu124 torch, preloaded ASR models |
| `clipflow-worker` | `worker/Dockerfile.gpu` | production — thin app layer, `FROM` the base above |
| — | `worker/Dockerfile` | **development/CI only.** CPU-only, no CUDA. Not referenced by Compose. |

The worker needs two images: a heavy base and a thin app layer on top. Compose expresses
that dependency with `additional_contexts: service:`, so `docker compose up -d --build`
builds them in the right order with no manual step.

To build the two stages separately — pre-warming the base in CI, rebuilding only the app
layer, or pushing the base to a registry — use the script:

```bash
scripts/build-worker.sh              # base + worker
scripts/build-worker.sh --base-only  # just the CUDA/torch/ASR base
scripts/build-worker.sh --app-only   # just the app layer (base must exist)
```

Build a CUDA-less worker (slower inference, no GPU required):

```bash
WORKER_TORCH_FLAVOR=cpu scripts/build-worker.sh
```

## Health verification

```bash
docker compose ps                       # every service should be running/healthy

curl -fsS http://localhost:8010/health  # clipflow-api liveness
curl -fsS http://localhost:8010/ready   # clipflow-api readiness (checks the database)
curl -fsS http://localhost:8000/health  # control-plane liveness
curl -fsS http://localhost:8000/ready   # control-plane readiness

docker compose logs -f voxmind-worker   # expect "VOXMIND WORKER READY — waiting for jobs"
```

`clipflow-api` and `voxmind-control-plane` have Compose healthchecks. The worker waits for
`clipflow-api` to report **healthy** before starting, so it never comes up mid-migration.

Ports: API `:8010`, control-plane `:8000`, MinIO `:9000` (+ console `:9001`, loopback only).
Postgres and Redis are bound to `127.0.0.1` — override `POSTGRES_BIND_HOST` /
`REDIS_BIND_HOST` if you genuinely need them reachable from another machine.

## Worker runtime

Jobs are claimed from Redis with a reliable-queue pattern, so a crashed worker never
destroys a job.

```
voxmind_jobs                pending work
      │ BLMOVE (atomic claim) + lease
      ▼
voxmind_jobs:processing     in-flight, one entry per claimed job
      │
      ├── success ──────────▶ removed (ACK) + workdir deleted
      ├── retryable failure ─▶ voxmind_jobs:delayed  (backoff, attempt+1) ─▶ pending
      └── exhausted /
          non-retryable ────▶ voxmind_jobs:dead      (payload + failure metadata)
```

**Invariant:** a payload leaves `processing` only through an explicit acknowledge, retry or
dead-letter. If a worker dies mid-job the payload stays in `processing`; its lease expires
because nothing renews it, and the next worker's sweep requeues it.

Inspecting the queue:

```bash
docker exec voxmind-redis redis-cli LLEN voxmind_jobs             # waiting
docker exec voxmind-redis redis-cli LLEN voxmind_jobs:processing  # in flight
docker exec voxmind-redis redis-cli ZCARD voxmind_jobs:delayed    # waiting to retry
docker exec voxmind-redis redis-cli LLEN voxmind_jobs:dead        # dead-lettered
docker exec voxmind-redis redis-cli LRANGE voxmind_jobs:dead 0 -1 # why they failed
```

Which workers are alive, and what they are doing:

```bash
docker exec voxmind-redis redis-cli KEYS 'clipflow:workers:*'
docker exec voxmind-redis redis-cli GET clipflow:workers:<worker_id>
```

Each worker refreshes that key on `WORKER_HEARTBEAT_INTERVAL_SEC` and it expires after
`WORKER_HEARTBEAT_TTL_SEC`, so a dead worker disappears on its own. The same loop renews the
lease of the job it is running.

### Logs

Every log line carries `job_id`, `pipeline_stage`, `step`, `status`, `attempt` and
`worker_id`, so one run is reconstructible:

```bash
docker compose logs voxmind-worker | grep '"job_id": "<id>"'
```

```json
{"levelname": "INFO", "message": "Job claimed",     "job_id": "abc", "step": "queue_claim",  "status": "claimed",   "attempt": 1, "worker_id": "worker-1"}
{"levelname": "INFO", "message": "transcribe:completed", "job_id": "abc", "step": "transcribe", "status": "completed", "attempt": 1, "worker_id": "worker-1"}
{"levelname": "INFO", "message": "Job acknowledged","job_id": "abc", "step": "queue_ack",    "status": "acknowledged","attempt": 1, "worker_id": "worker-1"}
```

External tools (yt-dlp, ffmpeg, ffprobe) run under per-tool timeouts. A successful run logs
nothing extra; a failed one logs the captured, truncated stderr against the same `job_id`.

## Pipeline flows

- **Manual (`--manual`)**: `/new --manual <url>` → prepare → the operator returns a
  `response.json` → `/finalize` → cuts.
- **Automatic (default)**: `/new <url>` → prepare → the AI provider router generates the
  cuts → finalize is enqueued automatically → cuts.

`LOCAL_LLM_ENABLED=false` (the default) contacts no local node. With it enabled but
unreachable, the router falls back to OpenAI — the platform never requires the local node
to be online.

## Transcription of long videos

Long audio is transcribed in windows. Windows **overlap**, so a sentence spoken across a
boundary is not cut in half by the extraction:

```
window 0: [0, 900)
window 1: [895, 1795)   shared with window 0: 895..900
window 2: [1790, 2690)  shared with window 1: 1790..1795
```

The shared region is transcribed twice and then reconciled. Which copy survives is decided
by where the window edges fall, not by a similarity score alone:

* a segment running into a window's **right** edge was cut off there — the next window saw
  the whole sentence, so its version wins;
* a segment starting at a window's **left** edge was cut off there — the previous window had
  the preceding audio, so its version wins;
* otherwise the more complete transcription wins, ties broken toward the earlier window.

Complementary halves of one utterance are not duplicates and both survive. Reconciliation
only ever *chooses between* transcriptions the model produced — it never invents words.

`ASR_WINDOW_OVERLAP_SEC=5` costs about 0.6% extra audio on a 90-minute video
(`5430s` processed instead of `5400s`). All seam thresholds live in one policy object; see
`worker/app/media/seam_reconciler.py`.

**Windows are transcribed sequentially.** `ASR_PARALLEL_WORKERS` is passed to the model as
`num_workers` (intra-window batching) and does not run windows concurrently. Making them
concurrent would complicate checkpointing and risk GPU OOM, so it is deliberately left for a
separate change.

Checkpoints are per window and carry the window range plus a hash of the algorithm that
produced them, so a resume after a configuration change re-transcribes instead of splicing
mismatched offsets. Checkpoints written before overlap existed are recognised and discarded
rather than silently reused. The MinIO transcript cache is versioned on the same inputs.

Run the seam evaluation:

```bash
python -m evaluation --asr
```

## Cut quality evaluation

Editorial changes are measured, not eyeballed. The harness runs the real analysis chain,
prompt assembly, structural validation, normalization, cutter planning and QA offline — no
Redis, MinIO, Telegram, network, download, ffmpeg or real provider.

```bash
docker run --rm -v "$PWD/worker:/w" -w /w -e MINIO_ENDPOINT=minio:9000   -e MINIO_ROOT_USER=x -e MINIO_ROOT_PASSWORD=y python:3.11-slim   sh -c "pip install -q poetry==2.0.1 && poetry config virtualenvs.create false          && poetry install --no-root --only main -q          && python -m evaluation --out evaluation/baselines/evaluation_after.json"
```

Compare two runs with the same metric definitions:

```bash
python -m evaluation --compare   evaluation/baselines/evaluation_before.json   evaluation/baselines/evaluation_after.json
```

Metrics are decomposable on purpose — boundary quality, duration-contract failures, silent
cut drops, candidate coverage, span grounding, structural AI validity, speaker continuity
and QA states are reported separately, so a regression in one cannot be masked by a gain in
another. Anything the dataset cannot support is reported as unmeasurable rather than as a
pass.

Cases live in `worker/evaluation/datasets/voxmind/`; see that directory's README for how to
add a real one. The current corpus is entirely synthetic.

## Development

Tests run in Docker, so no host toolchain is required:

```bash
docker run --rm -v "$PWD/clipflow-api:/w" -w /w python:3.11-slim \
  sh -c "pip install -q poetry==2.0.1 && poetry config virtualenvs.create false \
         && poetry install --no-root -q && python -m pytest -q"
```

Substitute `control-plane` or `worker` for the other suites. The worker suite currently
needs a reachable MinIO for 6 of its tests.
