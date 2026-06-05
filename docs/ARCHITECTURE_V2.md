# ClipFlow / Voxmind V2 — Architecture

> Status: **living document**. Phase 0 of the V2 evolution. Describes the system as it exists
> today and the target V2 module map, marking what already exists vs. what is new/deferred.

## 1. Vision

ClipFlow becomes an **autonomous, observable AI content factory**. An operator configures
one or more **themes** (AI, Programming, Cybersecurity, Football, Finance, …). The platform
then runs indefinitely: discovering trending videos, ranking them, generating cuts, and
publishing — while the **frontend acts as an Operations Center** that always answers
*"what is the platform doing right now?"*.

The iPad Pro M4 (OpenClaw iOS node) is **optional**: if disconnected, the platform keeps
operating. OpenClaw is an **automation node**, never a place for business logic — the backend
is the source of truth.

## 2. Deployment reality

Everything runs through **Docker Compose** on a single main server (Core i5). There is **no
Kubernetes** anymore. The `k8s/` directory and older README text are historical and should be
treated as deprecated (physical removal is a later cleanup, out of scope for Phase 0–3).

Compose services (`docker-compose.yml`): `redis`, `minio` (+ `minio-init`),
`clipflow-postgres`, `clipflow-api`, `voxmind-worker` (GPU), `voxmind-control-plane`,
`clipflow-studio`.

## 3. Current architecture (today)

```
 Telegram ──/new──▶ control-plane ──LPUSH──▶ Redis queue ──BRPOP──▶ worker (GPU)
   ▲  │                (bot.py)              (voxmind_jobs)         (pipeline.py)
   │  └──/finalize (response.json)──────────────────────────────────────┐    │
   │                                                                     │    ▼
   └──────────── prompt.txt / clips / status ◀── Telegram notifs ◀───────┘  MinIO (artifacts)
                                                                              │
 clipflow-studio (React) ──HTTP/SSE──▶ clipflow-api (FastAPI) ──▶ Postgres ◀─┘ (artifact sync)
```

### 3.1 control-plane (`control-plane/app`)
- `bot.py` — Telegram bot. `/new [flags] <url>` enqueues a **prepare** job; a `response.json`
  document (or `/finalize`) enqueues a **finalize** job. Flags: `--short|--long|--long-series|
  --short-serie`, `--portrait|--landscape`, `--build-ia`.
- `queue_publisher.py` — `LPUSH`es the job payload to Redis. `job_registry.py` maps job_id →
  source url. `health_server.py` — readiness.

### 3.2 worker (`worker/app`)
- `main.py` — `BRPOP` loop (`WORKER_MODE=queue`) or `scheduler` mode that claims due
  `private_scheduler` runs and re-enqueues them. `run_pipeline()` dispatches by
  `pipeline_stage` (`prepare` | `finalize`) and uploads artifacts to MinIO, then calls the
  API sync endpoint.
- `pipeline/pipeline.py` — the two-stage `Pipeline`:
  - **prepare**: download → faster-whisper transcribe → (optional) diarize → chunk → hook /
    audio-peak / story-shift detection → candidate build → score → span catalog → build
    prompt → (manual) send to Telegram **or** (`--build-ia`) call OpenAI → `awaiting_manual_llm`.
  - **finalize**: validate AI JSON → cut (`video/cutter.py`) → render (`final_renderer.py`) →
    QA (`video/qa.py`) → auto-review → delivery/publish packages → upload + notify.
- `integrations/ai_client.py` — **OpenAI `gpt-4o-mini` client already exists**.
  `prompt_router.py` routes manual vs API. `clipflow_api_client.py` — best-effort calls to the
  API (`sync_job_artifacts_safe`, `update_runtime_safe`, `claim_due_private_scheduler_runs_safe`).

### 3.3 clipflow-api (`clipflow-api/app`)
- FastAPI + SQLAlchemy 2.0 + Alembic. Models under `app/models`, routers under `app/api`
  (registered in `api/router.py`). Auth via JWT cookie; internal calls via `X-Internal-Token`
  (`security/auth_middleware.require_internal_api_token`).
- Job lifecycle model `ClipJob` (`models/clip_job.py`) is **billing-coupled** (requires
  `purchase_id`, `product_id`, `user_id`). Status enum `JobStatus`
  (PENDING_PAYMENT…COMPLETED/FAILED). Per-step runtime is stored in `metadata_json.runtime`.
- **Realtime already exists via SSE**: `api/job_events.py` exposes `GET /jobs/{id}/events`
  (history) and `GET /jobs/{id}/stream` (SSE, polls Postgres every 2s, emits `job_update`).
  Worker → API events come in via `POST /internal/jobs/{id}/events` and `…/runtime`.
- A **scheduler already exists**: `private_scheduler_profile` / `private_scheduler_run` models
  + `services/private_scheduler_service.py` + `POST /internal/private-scheduler/claim-due`.

### 3.4 clipflow-studio (`clipflow-studio/src`)
- React + Vite + Tagged shadcn/ui + TanStack Query. `hooks/useJobRealtime.ts` consumes the
  per-job SSE stream. Pages: Dashboard, NewJob, JobDetail, ScriptStudio, AdminScheduler,
  AdminSecurity.

## 4. Target V2 module map

The brief's modules map onto existing code as follows. V2 is an **evolution**, not a rewrite.

| V2 module | Responsibility | Maps to |
|---|---|---|
| **clipflow-api** | auth, dashboard, topics, sources, jobs, pipeline state, **event hub** | existing `clipflow-api` (+ new ops/event endpoints) |
| **clipflow-scheduler** | periodic scan, retries, cooldowns, queue feeding | existing `private_scheduler_*` + worker `scheduler` mode (extend) — *deferred* |
| **clipflow-discovery** | trending search, news, dedup, normalization | **new abstraction** (`DiscoverySource` table only for now) — *deferred* |
| **clipflow-intelligence** | relevance / trend / duplicate / quality scoring → ranked candidates | **new** (`VideoCandidate.scores`); scoring logic exists in `worker/app/pipeline/scorer.py` — *deferred* |
| **clipflow-worker** | download → … → render | existing `worker` (refactor to emit states/events — *deferred to Phase 5*) |
| **clipflow-ai** | `AIProvider` abstraction (`generateCuts`, `generateMetadata`, `generateThumbnailPrompt`) | existing `worker/app/integrations/ai_client.py` (formalize — *deferred to Phase 6*) |
| **clipflow-publisher** | publication requests, history, retries, provider abstraction | **new** (`PublishTarget`, `PublishAttempt` tables); Telegram delivery exists — *deferred to Phase 7* |

## 5. Two parallel job lineages

V2 introduces a **second job lineage** that converges on the same Redis queue + worker:

- **`ClipJob`** (existing) — paid/manual jobs created by users via the API or Telegram. Coupled
  to `purchase`/`product`. **Unchanged** by V2.
- **`PipelineJob`** (new) — autonomous jobs created by the scheduler from a `VideoCandidate`.
  **No buyer**, no billing. Carries the granular `PipelineState` machine.

Both produce the same worker payload shape (`video_url`, `job_id`, `pipeline_stage`,
`clip_mode`, …) and reuse the identical worker pipeline. They differ only in *who creates them*
and *how they're tracked/observed*.

## 6. Pipeline state machine (V2)

`PipelineJob.state` advances through (brief §"PIPELINE STATE MACHINE"):

```
DISCOVERED → SELECTED → DOWNLOADING → DOWNLOADED → TRANSCRIBING → TRANSCRIBED
 → ANALYZING → PROMPT_BUILDING → WAITING_AI → AI_COMPLETED → RENDERING → RENDERED
 → READY_TO_PUBLISH → PUBLISHING → PUBLISHED
(any non-terminal) → FAILED | CANCELED ;  FAILED → DOWNLOADING (retry)
```

Every transition is validated by `services/pipeline_state_machine.py` and **emits a
`PipelineEvent`** (see §7). The existing coarse worker steps (`download_video`, `transcribe`,
`render_cuts`, …) map onto these states via `WORKER_STAGE_TO_STATE`.

## 7. Event model & realtime (V2)

A single generic event entity (`PipelineEvent`) is published by **every** service:

```jsonc
{ "id", "pipeline_job_id", "service", "stage", "type", "message", "payload", "created_at" }
```

- Services persist + fan out via `services/event_bus.publish_event(...)`, which writes the row
  **and** publishes a compact JSON to the Redis pub/sub channel `clipflow:events`.
- Non-API services (worker/scheduler/discovery/publisher) publish over HTTP:
  `POST /internal/events`.
- The frontend Ops Center subscribes to the **global SSE** endpoint
  `GET /ops/events/stream` (admin), which forwards the Redis channel. `GET /ops/events`
  returns history. This **extends** the existing per-job SSE pattern rather than introducing a
  WebSocket hub.

`PipelineEvent` is intentionally **distinct** from the legacy `JobEvent` (which stays bound to
`ClipJob`), so the paid flow is untouched.

## 8. OpenClaw & publishing (interfaces only)

OpenClaw is treated as a **browser/automation bot**. V2 only prepares **contracts** — no
implementation: a `Publisher` interface (`publishVideo`, `getPublishStatus`, `cancelPublish`)
and node operations (`InvokeNode`, `GetNodeStatus`, `ListNodes`, `PublishVideo`,
`TakeScreenshot`, `BrowserSessionStatus`). `ConnectedNode` + `PublishTarget` / `PublishAttempt`
tables back the ops panels. YouTube/TikTok/Instagram APIs are **not** implemented — publication
will be driven by an already-authenticated OpenClaw browser session. *(Deferred to Phase 7.)*

## 9. AI strategy (hybrid)

- **Transcription**: faster-whisper on the local GPU worker (today).
- **LLM**: OpenAI `gpt-4o-mini` API (already wired) replaces the manual prompt loop.
- **Decision engine (future)**: `IF local capability exists → local ELSE → OpenAI`. The
  `AIProvider` abstraction (Phase 6) makes `LocalProvider` / `OpenClawProvider` pluggable
  without refactoring callers. `AIExecution` records each call (provider, model, tokens, cost,
  latency) for the ops center.

### 9.1 AI provider layer (implemented)

The worker no longer depends on the manual Telegram prompt loop for AI. It calls a single
entrypoint — `worker/app/ai/provider_router.py` `ProviderRouter.generate_json(...)` — which:

- Uses the **local** provider (`local_provider.py`, optional iPad M4 / Ollama-compatible node)
  when `LOCAL_LLM_ENABLED` and its `/api/tags` healthcheck passes; otherwise falls back to the
  **OpenAI** provider (`openai_provider.py`, `gpt-4o-mini`, JSON output mode). The local node is
  optional — if down, the platform continues on OpenAI.
- Emits AI activity as generic `PipelineEvent`s (`service="ai"`, names in `payload.ai_event`:
  `AI_PROVIDER_SELECTED`, `AI_REQUEST_STARTED/FINISHED`, `AI_PROVIDER_FAILED`, `AI_FALLBACK`,
  `LOCAL_PROVIDER_ONLINE/OFFLINE`) through the existing EventBus → SSE → Ops Center
  "AI Providers" panel. No new event model; `pipeline_job_id` is null (global provider health).

Mode is per-job: `build_ia=true` (worker `AI_MODE=automatic` default) runs prompt → provider →
validate → auto-enqueue the existing **finalize** stage (no human). `build_ia=false` keeps the
legacy Telegram prompt/`response.json` flow exactly as before (fallback/debug). Prompt assembly
is composed from reusable parts in `worker/app/prompts/` (`builder.py`, `clip_modes/`,
`schemas/cuts_schema.json`); `worker/app/agents/` and `worker/app/graph/` are scaffolding for
future CrewAI/LangGraph orchestration.

## 10. Implementation phases

| Phase | Scope | Status |
|---|---|---|
| 0 | Map & document current architecture | **this document** |
| 1 | V2 domain models (`ContentTopic`…`AIExecution`) + migration | foundation increment |
| 2 | Pipeline state machine + tests | foundation increment |
| 3 | Event system + global SSE fan-out (+ thin Ops Center seed) | foundation increment |
| 4 | Full Ops Center dashboard | deferred (AI Providers panel added, see §9.1) |
| 5 | Refactor worker to emit states/events | deferred (AI section refactored, see §9.1) |
| 6 | Formalize `AIProvider` / `OpenAIProvider`, make API LLM the default | **done** (see §9.1) |
| 7 | OpenClaw `Publisher`/node abstraction | deferred |
| 8 | Discovery / intelligence abstraction + scheduler feeding `PipelineJob`s | deferred |
