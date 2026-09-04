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

## Content discovery

ClipFlow can find candidate videos instead of waiting for someone to paste a URL. Discovery
answers *"what content exists?"* — **not** *"what should we produce?"*. That second question
is selection, and it does not exist yet.

```
ContentTopic ─ queries, language, freshness
     └─> DiscoverySource ─ kind + config
              └─> Provider ──> DiscoveredVideo[]
                                    └─> dedup ──> VideoCandidate (DISCOVERED)
```

A discovery run **never** creates a PipelineJob and never writes a score. Candidates come to
rest in `DISCOVERED`; promoting one is a deliberate human action
(`POST /admin/video-candidates/{id}/select`), which exists so the boundary can be exercised
end to end and goes through the same service a future selector will use.

**Providers.** Two, so the interface is proven rather than assumed — with a single
implementation an abstraction is just that implementation with extra steps.

| Provider | Needs a credential | Notes |
|---|---|---|
| YouTube Data API v3 search | `YOUTUBE_API_KEY` | `search.list` → ids, then one batched `videos.list` for metadata |
| RSS / Atom | no | Covers YouTube channel feeds, which cost no quota |

Nothing is scraped: no HTML parsing, no headless browser, no quota circumvention. With no
key the YouTube provider reports itself **unavailable** and the API still boots — a missing
credential is never substituted with fake data.

**Quota.** A YouTube search costs 100 units of a 10,000/day allowance, so roughly 100
searches a day exist in total. Metadata for 50 videos costs 1 unit in a single batched call,
not 50. Repeated queries are deduplicated before they are sent, and a spent allowance is
classified non-retryable — it resets on a clock, not on a backoff, so retrying only spends
tomorrow's.

**Identity and dedup.** YouTube serves the same video under at least five URL shapes:

```
youtube.com/watch?v=ABC   youtu.be/ABC   youtube.com/shorts/ABC   .../embed/ABC   m.youtube.com/...
```

Five strings, one video. Identity is therefore `provider:external_id`, hashed into
`dedup_hash` under a **unique** partial index — two runs finding the same video at the same
moment are collapsed by the database, not by a read-then-write that races. Titles are never
part of identity: they collide both ways, since two channels title the same match alike and
one channel re-uploads under a new name.

**Idempotency.** Running the same discovery twice updates rather than duplicates.
`created_at` is never rewritten — a video rediscovered on its fifth day is not new — while
`last_seen_at` moves each time. A rediscovery never resets a status: a rejected candidate
stays rejected.

**Unknown is not zero.** A field a source does not publish is stored as `NULL`. An unknown
view count and a view count of zero are different facts.

Run it:

```bash
curl -X POST localhost:8010/admin/discovery/run      -H "Authorization: Bearer $ADMIN_JWT"      -d '{"topic_id": "..."}'

curl "localhost:8010/admin/video-candidates?status=discovered&limit=50"
```

Candidates are listed newest first (`published_at DESC`, `created_at` as tiebreaker) and
paginated. That is recency, not ranking — there is no scoring in this phase.

## Candidate selection

Discovery answers *"what content exists?"*. Selection answers *"what should we produce?"* —
and stops there. A selected candidate is an **editorial decision**, not an admission to
production: no `PipelineJob` is created and nothing is enqueued.

```
DISCOVERED
    ↓  eligibility      ← a gate with reason codes, never a penalty score
    ↓  pre-rank         ← cheap, deterministic; decides who is worth a model call
    ↓  semantic         ← top-K only, metadata only, optional
    ↓  composition      ← weighted mean over the signals that could be measured
    ↓  policy           ← thresholds, caps, diversity, cooldown
  RANKED → SELECTED
```

**Three questions, kept apart.** Eligibility asks whether a candidate may take part at all;
ranking asks which looks best; policy asks whether to act now. Collapsing them into one
number makes "why was this rejected?" unanswerable.

**Signals.** Freshness decays exponentially (24h half-life) rather than stepping, so a 23-hour
video does not beat a 25-hour one by the whole weight of the signal. Engagement is
age-normalised into `observed_average_views_per_hour` and log-compressed — 1M views over five
years is not 100k views in three hours. The name is literal: there are no engagement
snapshots yet, so this is a lifetime average, not current velocity and not acceleration.

**Unknown is not zero.** RSS feeds publish no view counts. Scoring those as 0 would bury every
RSS candidate beneath every YouTube one for a reason unrelated to the content, so an
unmeasurable signal has its weight removed from the denominator instead. That is fair, not
free: a candidate with less evidence must clear a higher bar
(`minimum_score_without_semantic`).

**The semantic leg is optional and cannot dominate.** It sees metadata only — there is no
transcript at this stage, and downloading one per candidate to decide whether to select it
would cost more than producing the video. Its output is a validated Pydantic contract. With no
provider configured it reports `unavailable` and the engine continues deterministically; there
is no local stand-in generating plausible numbers. Eligibility and policy are applied before
and after it, so a confident model cannot select an unavailable video or break a channel cap.

**Scores are versioned** (`selection-v1`). A 0.82 from a different formula is not the same
0.82, and `scores_json` records every signal, the weights actually used and what could not be
measured.

| Weight | V1 heuristic |
|---|---|
| relevance | 0.35 |
| trend | 0.30 |
| freshness | 0.20 |
| editorial interest | 0.15 |
| source priority | 0.05 |

These were chosen against the evaluation fixtures and rounded to two decimals. There are no
human labels to fit against, so anything more precise would be false precision.

**Policy** lives on the topic (`ContentTopic.metadata_json["selection"]`), not in environment
variables — it belongs next to the editorial intention:

```json
{"selection": {"freshness_hours": 72, "minimum_score": 0.45,
               "max_selected_per_run": 3, "max_per_channel": 1}}
```

A per-channel cap plus a cross-run cooldown stops one prolific channel filling the feed, and a
daily cap plus a server-side ceiling (25) bounds what automation can do. Committed runs take a
PostgreSQL advisory lock on the topic, so two concurrent runs cannot both spend the same cap.

**Only permanently unusable candidates are rejected.** A cap, a cooldown or today's freshness
window are temporary — rejecting on those would burn a candidate tomorrow's run should still
consider.

Run it, without changing anything:

```bash
python -m evaluation.selection --ranking          # offline, against fixtures

curl -X POST localhost:8010/admin/selection/run      -H "Authorization: Bearer $ADMIN_JWT"      -d '{"topic_id": "...", "dry_run": true}'     # dry run is the default
```

## Production admission

Selection decided *what* is worth producing. Admission decides *whether we can start it now*
— a different question with different inputs, and the last gate before the system spends GPU
time on its own.

```
VideoCandidate SELECTED
        ↓  status · availability · capacity · idempotency
        ↓  PipelineJob QUEUED        ← committed, carrying a frozen snapshot
        ↓  enqueue                   ← the payload reaches Redis
VideoCandidate CONSUMED              ← only after the handoff is real
```

**Ordering is the design.** The row commits *before* the enqueue and the candidate is marked
CONSUMED *after* it, so every crash point leaves something recoverable:

| Crash point | State left behind | Recovery |
|---|---|---|
| before commit | nothing happened | candidate still SELECTED |
| between commit and enqueue | run with `enqueued_at IS NULL` | `POST /admin/admission/retry-pending` |
| after enqueue, before status update | run live, candidate SELECTED | re-admitting returns `already_admitted` and repairs the status |

Enqueueing first would have no recoverable middle: a message would be in flight for a run
that does not exist. This is not exactly-once — it is at-least-once with a database-enforced
identity that makes the duplicate harmless.

**Idempotency.** Every admission carries `admit:<candidate_id>:v1` — deterministic, never
time-based, under a unique partial index. A retried request, two operators clicking at once
or two overlapping scheduled runs all resolve to one run, because the constraint settles it
rather than a `SELECT` that races. Bumping the profile suffix is how a *deliberate*
re-production of the same source is requested.

**Capacity** is a separate budget from the selection caps: selection limits what is chosen,
admission limits what is started. Active runs are counted from the state machine's own happy
path minus its resting states, so a state added there cannot silently stop counting.

| Limit | Default | Ceiling |
|---|---:|---:|
| `max_active_jobs` | 3 | 20 |
| `max_admissions_per_run` | 3 | 10 |
| `max_admissions_per_day` | 12 | — |

Configured per topic (`ContentTopic.metadata_json["admission"]`) and clamped server-side, so
a mistyped `10000` cannot become a runaway. Committed runs take a PostgreSQL advisory lock on
the topic, so two concurrent runs cannot both see the same free slot.

**Blocked is not rejected.** Capacity, the daily cap and the run limit are temporary — the
candidate stays SELECTED and is admissible later. Only a candidate that is unavailable or has
no URL is permanently blocked, and even then admission never changes its status: that is
selection's decision to make.

**Inputs are frozen at admission.** Clip mode, ratio, and the source URL are snapshotted onto
the run, so editing the topic an hour later cannot reshape a job already in flight. The run
also records its provenance — which candidate, which selection run, which score and score
version — and the candidate records where it went.

**Admission ends at the queue.** Nothing here publishes; a run reaching `READY_TO_PUBLISH`
still goes nowhere.

```bash
curl -X POST localhost:8010/admin/admission/run      -H "Authorization: Bearer $ADMIN_JWT"      -d '{"topic_id": "...", "dry_run": true}'      # dry run is the default

curl -X POST localhost:8010/admin/video-candidates/{id}/admit
curl -X POST localhost:8010/admin/admission/retry-pending
```

## Autonomous loop

Discovery, selection and admission each work on their own. The scheduler is what makes them a
system: a deterministic coordinator that decides *when* a topic runs, *which* stages run, and
*whether to continue* — and nothing else. It owns no scoring rule, no capacity rule and no
admission rule; every one of those still lives in the service that always owned it.

```
AutomationRunner   every AUTOMATION_POLL_INTERVAL_SEC, in the API process
        |
     tick()        recover pending enqueues -> pick the topics that are due
        |
   advisory lock   pg_try_advisory_lock per topic; unavailable -> skip, never wait
        |
  discovery -> selection -> admission        each attempted, none able to abort the rest
        |
   PipelineJob QUEUED -> Redis               the same handoff a manual admission makes
```

**It stops at the queue.** The loop can decide to produce; it cannot decide to publish.

### Scheduling

One `automation_states` row per topic holds `next_due_at`, so the schedule survives a restart:
a process that comes back up does not re-run everything, it reads what was already due. The
interval is per topic (`ContentTopic.metadata_json["automation"]`), floored at 5 minutes, and
each topic gets a deterministic jitter derived from `sha256(topic_id)` — stable across
restarts, but enough to keep ten topics from hitting the same provider in the same second.

A tick runs at most 5 topics; the rest stay due and are picked up by the next pass, so one
tick cannot monopolise the process.

### Three kill switches, and what each one stops

| Switch | Scope | Effect |
|---|---|---|
| `AUTONOMOUS_PIPELINE_ENABLED` | global, **default `false`** | no topic runs; the loop keeps ticking so it can be turned back on without a restart |
| `automation.enabled` | one topic | that topic stops starting new cycles |
| `automation.{discovery,selection,admission}_enabled` | one stage | that stage is skipped; the others still run |

The global switch defaults to off because the defensible default for a system that spends GPU
time on its own is that a fresh deployment does not. It also gates the legacy `seed_urls_json`
scheduler in `/internal/*`, so the two autonomous producers can never compete for worker slots.

Pausing is reversible and destroys nothing: statuses are untouched, running jobs keep running.

### Not running twice

Three independent guards, because they fail in different ways:

1. **`pg_try_advisory_lock` per topic** — across replicas. Non-blocking: an unavailable lock
   is a skip, never a wait, because the work will still be due on the next tick.
2. **`running_since`** — across ticks of the same process, for a run that outlived its own
   interval. Cleared after 60 minutes so a crashed run cannot wedge a topic forever.
3. **Idempotency in the services below** — the fallback that makes a duplicate harmless
   rather than merely unlikely.

The manual trigger goes through the scheduler too, so it takes the same lock as an automatic
run. It bypasses only the due check.

### Failure isolation

A stage that raises is recorded and the run continues: a discovery provider timing out must
not cost the candidates already selected their admission. The run's status says what happened
— `completed`, `partial`, `failed`, or `noop` — and **an empty run is not a failure**: a tick
that finds nothing new is the normal state of a working system.

Only the exception *type* is recorded, never its message, because a provider that interpolates
its own request into an error would put an API key in the event log.

**Backpressure.** Selection is capped by how many candidates are already SELECTED and waiting,
so the backlog cannot grow without bound while admission is at capacity. Capacity blocking is
temporary and does not fail a run.

```bash
curl localhost:8010/admin/automation/status        -H "Authorization: Bearer $ADMIN_JWT"
curl -X POST localhost:8010/admin/automation/tick  -H "Authorization: Bearer $ADMIN_JWT"
curl -X POST localhost:8010/admin/automation/topics/{id}/run
curl -X PUT  localhost:8010/admin/automation/topics/{id} -d '{"enabled": true, "interval_minutes": 60}'
```

All four are admin-only, on the same RBAC as every other `/admin/*` route. Configuration
changes are audited; ticks are not — they are events and logs, not human decisions.

## Publishing

The autonomous loop stops at `READY_TO_PUBLISH`. Getting past that line takes a named admin
and an explicit command — nothing in this system publishes on its own.

```
READY_TO_PUBLISH
      |
POST /admin/pipeline-jobs/{id}/publish        ← an admin, deliberately, dry_run=false
      |
  preflight        kill switches · target · QA eligibility · media · metadata
      |
 PublishAttempt    committed BEFORE any byte leaves          → 202 Accepted, request ends
      |
 publish queue → clipflow-publisher                          claimed atomically
      |
 YouTube Data API v3, resumable upload, streamed in 256 KiB chunks
      |
  external_id → PUBLISHED
```

**Why the ordering is the reverse of admission's.** Admission commits a row then enqueues,
because a lost message is recoverable. Publishing commits the attempt *before* the first byte
and writes the external id *after* the provider confirms, because the thing that must never
happen is a video existing that this system has no record of.

### The publication runtime

The upload does not happen inside the HTTP request. A large clip can take longer than any
proxy timeout between an operator and the API, and the publication carried on regardless — so
the request reported a failure that had not happened.

```
POST .../publish (dry_run=false)   → 202 Accepted, returns immediately
        |
   PublishAttempt committed, command enqueued
        |
   clipflow_publish_jobs (Redis)   ready → processing → delayed → dead, plus leases
        |
   clipflow-publisher              its own container: no GPU, no port, own restart cycle
        |
   YouTube resumable upload
```

Its own queue, never the media queue: a render retry costs GPU time, a publication retry can
cost a duplicate public video, so the two disagree about the most important setting they would
have to share. The publisher runs from the API image (that is where the code is) but as a
separate service — it must not be interrupted by an API deploy, and must not queue behind
ffmpeg and ASR.

**Delivery is at-least-once; the publication is not.**

```
Redis command delivery      at-least-once      a command can arrive twice
PublishAttempt execution    idempotent         the atomic claim allows one upload
External publication        at-most-once       guarded, never guaranteed by the queue
Ambiguous external result   UNKNOWN            a human or a session probe settles it
```

There is no exactly-once upload to YouTube and this system does not claim one.

### Two leases, two authorities

The distinction the runtime is built around:

| Question | Answered by |
|---|---|
| which worker owns this command? | the **queue lease** |
| is it safe to call the provider again? | the **attempt row** |

Deriving the second from the first — "the lease expired, so retry" — is what turns a dead
worker into a second video. A recovered command is never simply re-run; it is classified from
evidence written before each irreversible step.

### Crash recovery

`provider_started_at` is committed immediately before the first call that could create anything
at the provider. The resumable session URI and the byte offset are committed **as the upload
proceeds**, not when it ends — otherwise a worker killed mid-upload leaves no session, recovery
concludes nothing durable exists, and the next execution starts a second one.

| Crash point | Evidence on the row | Recovery |
|---|---|---|
| before the provider | no `provider_started_at`, no session | requeue — nothing exists remotely |
| session opened, no bytes | `provider_started_at`, no bytes | requeue — an unused session is not a video |
| mid-chunk | session + offset | probe → 308 → **resume the same session** |
| final chunk | session + full offset | probe: completed → success; expired → **UNKNOWN** |
| provider succeeded, DB not yet written | attempt `IN_PROGRESS` | probe returns the video → recorded |
| DB written, ACK lost | attempt terminal | redelivery finds it settled, zero provider calls |

A worker that stalls, loses its lease, and later wakes up can neither acknowledge the command
(the queue settles under a compare-and-set on an owner token) nor overwrite the newer outcome
(the record path refuses to write unless it still owns the attempt).

### Retry budget

`PUBLISH_MAX_ATTEMPTS` (default 3) is the **total**: the provider client does not retry
internally, so nothing multiplies it. Backoff is exponential with jitter — several
publications of one run fail together when a provider has a bad minute, and without jitter
they return together and fail together again.

| Failure | Next |
|---|---|
| 503 / 429 / network | delayed retry, same attempt row, `attempt_no + 1` |
| `quotaExceeded` | delayed by `PUBLISH_QUOTA_BACKOFF_SEC` (1h) — a daily quota does not clear in 30s |
| `invalid_grant` | target → `reconnect_required`, attempt final, command acknowledged |
| budget exhausted | dead letter (the command), attempt left for an operator |
| **UNKNOWN** | **acknowledged, never retried** |

The dead letter is for commands the runtime could not process. An `UNKNOWN` publication is not
a runtime failure — it is a correctly recorded outcome that needs a person, and it lives in the
database where one can act on it.

### Recovering an enqueue that never happened

Moving execution behind a queue reopens the window admission has: the row commits, then Redis
is unreachable. The attempt keeps `enqueued_at IS NULL` and a bounded sweep sends the command
later. That sweep reads `publish_attempts` and nothing else — it never queries for runs in
`READY_TO_PUBLISH`, because a sweeper that could create publications would be autopublish
wearing a disguise.

### Is it running?

```bash
curl localhost:8010/admin/publishing/runtime -H "Authorization: Bearer $ADMIN_JWT"
```

```json
{"ready": 2, "processing": 1, "delayed": 0, "dead": 0,
 "workers": [{"worker_id": "publisher-...", "last_heartbeat_at": "..."}],
 "workers_alive": 1, "pending_enqueue": 0, "unresolved": 0}
```

`workers` comes from Redis heartbeats with a TTL, never from configuration: a dead publisher
otherwise looks exactly like an empty queue, with every publish accepted and none executed.

### The outcome vocabulary

Most integrations model an upload as success-or-failure. That is exactly the model that
duplicates videos.

| Outcome | Means | What may happen next |
|---|---|---|
| `SUCCEEDED` | the provider returned a video id | nothing |
| `FAILED_RETRYABLE` | nothing was accepted (503, 429, quota, a drop mid-upload) | bounded retry on the same row |
| `FAILED_FINAL` | would fail identically (bad metadata, revoked credentials) | a human changes something |
| `UNKNOWN` | **bytes were sent and the response was lost** | reconcile, or a human — *never* an automatic retry |

`UNKNOWN` is the reason this PR exists. There is no idempotency key for `videos.insert`, so a
retry after a lost response uploads a second video. The adapter decides by *where* a request
died, not which exception it raised: a timeout opening the session created nothing; a timeout
on a middle chunk is resumable; a timeout on the **final** chunk may mean the video already
exists.

### Safety invariants

```
technical QA failure          → never publish
publication_eligibility false → never publish
PUBLISHING_ENABLED=false      → never publish
target disabled or unconnected→ never publish
unknown outcome               → never blind retry
same logical publication      → at most one confirmed external video
```

Eligibility is re-read from the run at publish time, not inferred from its state: a manual
endpoint must not be able to bypass the gate by arriving at a run whose state was set some
other way. A run with no eligibility record is refused — an unmeasured gate is not a passed
gate.

### Idempotency

Every publication carries `publish:<job_id>:<target_id>:<media_identity>:v1`, deterministic
and never time-based, under a unique index. Two protections, because they stop different
things:

- the **unique index** deduplicates the *row* — two concurrent requests, one attempt;
- an atomic **conditional claim** (`UPDATE ... WHERE status IN (claimable)`) deduplicates the
  *upload* — the request that loses reports `in_progress` and sends nothing.

A retry does not create a second row; it increments `attempt_no` on the existing one.

### Media and metadata

One PipelineJob can render several final clips, and `publish_package.json` gives each its own
title, description and hashtags — so **each generated final clip is one publication**.
`final_reel.mp4` is the review artifact and is not published. The run reaches `PUBLISHED` only
when every required publication is confirmed; anything less is reported as `partial` or
`unresolved` and the successes are kept.

Metadata precedence, recorded on every attempt so "why this title" is answerable from the row:

```
explicit request  >  publish_package.json  >  target defaults  >  system defaults
```

Values are validated rather than silently repaired. An over-long title **blocks** the
publication (a person wrote it to be read whole); a description over the API limit is
truncated at a word boundary and the fact is recorded. Privacy defaults to `private`. No
category is invented — there is no YouTube category meaning "football highlights", and
guessing one mis-files every video on the channel. `made_for_kids` is configuration, never
inferred from content. The metadata sent is frozen on the attempt, so a re-render cannot
change what a retry uploads.

### OAuth and secrets

Scopes are the minimum that works: `youtube.upload` to upload and `youtube.readonly` to
resolve which channel the token actually reaches — not `youtube.force-ssl`, which would also
grant deleting playlists, comments and captions. The `state` parameter is 32 CSPRNG bytes,
single-use, expiring, and bound to the admin who started the flow; it lives in `oauth_states`
rather than a cookie because the callback can land on a different replica.

Refresh tokens are encrypted at rest with Fernet (`PUBLISH_SECRET_KEY`), and so is the
resumable session URI — it is a bearer credential that looks like an ordinary URL. Neither is
in any API response, any log line, or any stored error message: only a provider *error code*
is kept, because Google echoes request parameters into some error descriptions. A refresh that
returns `invalid_grant` marks the target `reconnect_required` and drops the dead ciphertext.

A freshly connected target is **disabled**. Consent proves the credential works, not that it
is the channel the operator meant.

```bash
curl -X POST localhost:8010/admin/publish-targets/youtube/connect   # → authorization_url
curl -X PUT  localhost:8010/admin/publish-targets/{id} -d '{"is_active": true}'

# a dry run is synchronous: it touches no provider, so there is nothing to wait for -> 200
curl -X POST localhost:8010/admin/pipeline-jobs/{id}/publish -d '{"target_id": "..."}'

# a real publication is accepted and executed by the publisher container -> 202
curl -X POST localhost:8010/admin/pipeline-jobs/{id}/publish      -d '{"target_id": "...", "dry_run": false, "privacy": "private"}'

curl localhost:8010/admin/publish-attempts/unresolved                # the operator's queue
curl -X POST localhost:8010/admin/publish-attempts/{id}/reconcile    # ask the session
curl -X POST localhost:8010/admin/publish-attempts/{id}/resolve -d '{"external_id": "..."}'
```

Every route is admin-only on the existing RBAC. The OAuth callback is the one exception and
cannot be otherwise — the browser arriving from Google carries Google's session, not ours;
its authorisation is the single-use `state`.

## Autonomous publication

The loop can now close without a person in it — but only when every gate says so. Autopublish
does not mean "a run in `READY_TO_PUBLISH` should be published". It means "a run may be
published without a human *only* when all of these hold, and each one can actually be
checked". A gate that cannot be evaluated has not passed.

```
READY_TO_PUBLISH
      |
AutonomousPublicationService     policy only: it uploads nothing and knows no provider
      |
the SAME publish command an admin issues    (initiator = automatic)
      |
publish queue  ->  clipflow-publisher  ->  YouTube  ->  PUBLISHED
```

It is not a second publisher. Reusing the manual path is the point: the QA gate, the
idempotency key, the atomic claim and the UNKNOWN rule are what make publishing safe, and a
parallel implementation would be a parallel set of ways to get them wrong. The scheduler calls
one application service; it holds no credential and imports no adapter.

### Every gate

| Gate | If it fails |
|---|---|
| `PUBLISHING_ENABLED` | nothing publishes, manual or automatic |
| `AUTOPUBLISH_ENABLED` | manual publishing still works; automatic stops |
| topic `automation.enabled` | that topic runs nothing |
| topic `automation.autopublish_enabled` | that topic never publishes itself |
| topic `automation.publish_target_id` | `publish_target_not_configured` — no implicit channel |
| target `is_active` / connected | `target_inactive` / `target_disconnected` |
| target `autopublish_enabled` | `target_autopublish_disabled` |
| state is `READY_TO_PUBLISH` | `REVIEW_REQUIRED`, `FAILED`, `CANCELED` are never candidates |
| `publication_eligibility.eligible` | `publication_ineligible`; **missing** → `publication_eligibility_missing` |
| no unresolved attempt | `unresolved_attempt` — never publish over an UNKNOWN |
| no `FAILED_FINAL` / `CANCELED` attempt | `previous_final_failure` / `operator_canceled` |
| ready after the cutoff | `historical_before_autopublish_cutoff` |
| privacy allowed | `public_autopublish_disabled` |
| daily and per-tick caps | `daily_limit_reached` / `per_run_limit_reached` |
| a publisher is alive | `publisher_unavailable` |
| queue not saturated | `publish_queue_backpressure` / `publish_dead_letter_backpressure` |

No decision here consults an LLM. Every gate is a comparison against persisted state.

**A pause is not a refusal.** Reasons split in two, and the distinction is reported per
candidate as `manual_publish_still_allowed`. `daily_limit_reached` or
`historical_before_autopublish_cutoff` mean the system is declining to act on its own — a
human may still publish. `publication_ineligible` or `unresolved_attempt` mean the output is
not fit to publish, and publishing it by hand would be overriding a safety gate.

### Three switches, not one

```
PUBLISHING_ENABLED=false                                    nothing publishes
PUBLISHING_ENABLED=true  AUTOPUBLISH_ENABLED=false          manual only
PUBLISHING_ENABLED=true  AUTOPUBLISH_ENABLED=true           automatic, private
                         AUTOPUBLISH_PUBLIC_ENABLED=true    automatic, public
```

All three default to **false**. Separating them is what makes a careful rollout possible:
"publish automatically" and "publish automatically to the whole internet" are different risks,
and collapsing them would remove the stage where automation runs for days without anything
becoming visible.

Privacy comes from the target's `default_privacy`, else `AUTOPUBLISH_DEFAULT_PRIVACY`
(`private`). It is never inferred from content and never read from `publish_package.json` — a
distribution decision is not editorial metadata, and letting the render pipeline choose it
would put "who can see this" downstream of an LLM. The public guard is checked on the
*resolved* value, so a target default of `public` cannot route around it.

### Which channel

`ContentTopic.metadata_json.automation.publish_target_id`, explicitly. Never "the first active
YouTube target": that rule silently changes meaning the day a second channel is connected, and
the failure is a video on the wrong audience, which cannot be taken back.

### The backlog that is not published

Enabling autopublish on a channel with fifty finished runs waiting must not publish fifty
videos. Switching it on stamps `autopublish_enabled_at`, and only runs that became publishable
after that moment are automatic. The older ones stay publishable by hand.

The comparison uses `first_ready_at`, recorded once by the state machine — `finished_at` is
refreshed when a failed publication releases a run back to `READY_TO_PUBLISH`, so a month-old
run would otherwise look brand new.

### Caps

| Cap | Default | Ceiling |
|---|---:|---:|
| `AUTOPUBLISH_MAX_PER_TICK` | 1 | 10 |
| `AUTOPUBLISH_MAX_PER_DAY` | 3 | 50 |
| topic `automation.autopublish_limit` | 1 | 5 |

Clamped server-side, because configuration is an operator's intent and not a licence: a
mistyped `100000` becomes a small number rather than a channel full of videos. The daily cap
counts **logical publications** — one attempt row per job/target/media — so a retry, a queue
redelivery or a provider call does not spend it again, and a run producing three clips counts
as three. Manual publications do not consume the automatic cap.

Candidates are ordered by `first_ready_at` ascending: deterministic, from persisted state, and
the run that has waited longest goes first.

### Backpressure

Automation checks the publisher fleet's heartbeat before creating anything. With no publisher
alive it queues nothing — otherwise a dead fleet accumulates a day of uploads that all arrive
at once when one starts. It also pauses when the queue is saturated or the dead letter has a
real pile in it (not on a single old entry, which must not be able to stop publishing forever).

Manual publishing is deliberately unaffected by these: a human watching a request is a
different situation from a loop firing unattended.

### Provenance

```json
{"initiator": "automatic",
 "provenance": {"policy_version": "autopublish-v1",
                "autopublish_run_id": "...", "automation_run_id": "...", "topic_id": "..."}}
```

On the attempt row, not reconstructed from the audit log later: "was this video published by a
person?" is a question about the publication itself.

```bash
curl -X POST localhost:8010/admin/autopublish/run -d '{}'                 # dry run, the default
curl -X POST localhost:8010/admin/autopublish/run -d '{"dry_run": false}'
curl localhost:8010/admin/autopublish/status
curl -X PUT localhost:8010/admin/publish-targets/{id} -d '{"autopublish_enabled": true}'
```

### Rollout

1. `PUBLISHING_ENABLED=true`, `AUTOPUBLISH_ENABLED=false` — publish by hand, confirm the
   channel and the metadata are right.
2. Enable the target and the topic, then `AUTOPUBLISH_ENABLED=true` with
   `AUTOPUBLISH_PUBLIC_ENABLED=false`. The system publishes privately, unattended. Watch
   `/admin/autopublish/status` and the private uploads for as long as it takes to trust them.
3. Only then `AUTOPUBLISH_PUBLIC_ENABLED=true`, and set `default_privacy` on the specific
   targets that should go public.

## Operational health

`/health` and `/ready` answer *is this container working* — they are what an orchestrator
watches. `GET /admin/operations/health` answers *is the product working*. The two are
deliberately different endpoints: a YouTube token expiring is a real problem and a real signal,
and it must never make Docker restart a perfectly healthy API.

For the same reason it returns **200 with a status field**, not a 5xx. The request succeeded;
the answer is that the product is degraded.

```json
{"status": "degraded",
 "signals": [{"code": "unresolved_publications", "severity": "high", "active": true,
              "message": "1 publication(s) need a human decision",
              "metadata": {"count": 1, "oldest_age_sec": 7200}}]}
```

| Signal | Condition | Severity |
|---|---|---|
| `publisher_down` | publishing on, work waiting, no publisher alive | critical |
| `unresolved_publications` | any `UNKNOWN` / `NEEDS_MANUAL_RESOLUTION` attempt | high |
| `publish_dead_letters` | dead letter at or above the threshold | high |
| `stalled_publish_queue` | a publication has waited past the window with a live publisher and none settling | high |
| `automation_runner_stale` | automation enabled, no runner heartbeat | high |
| `target_reconnect_required` | an **active** target has lost its credential | high |
| `repeated_automation_failure` | a topic's consecutive failures at or above the threshold | medium |

Signals are derived each time they are asked for. Nothing is materialised and nothing needs
acknowledging: when the condition stops holding, the signal stops being active. A stored alert
that outlives its cause is one operators learn to ignore.

Every one is written not to cry wolf. Publishing switched off does not raise `publisher_down`.
A deep queue that is draining is not a stall. An inactive target that needs reconnecting is
nobody's problem. One dead letter is not a pile.

### Is the loop actually running?

`GET /admin/automation/status` now separates two things that used to look identical:

```
runner_enabled   configuration: this process was TOLD to run a loop
runner_state     evidence: disabled | live | stale
last_tick_at     when a loop last completed a tick
```

The runner writes a Redis heartbeat **after** each tick — after, so a loop whose every tick
raises does not keep reporting itself healthy. A configuration flag could never say this, and
before PR-AUTONOMY-HARDEN-01 it was the only thing on offer.

## Publication completion

`PUBLISHED` means *every media item this run was required to publish has been published*. It
used to mean *every PublishAttempt that happens to exist succeeded* — which is true of a
four-clip run with two attempts, and marked such runs PUBLISHED with two videos never uploaded
and nothing left to say so.

Three things are now distinguished, where two used to be conflated:

```
required     what the run owes, from a manifest fixed before anything was published
attempts     what has been tried, on one target
outstanding  required items with no attempt at all - the only ones anything may create
```

### The manifest

`publish_package.json`'s `videos[]` is the contract for "one publishable output". It is read
**once**, on first access — which is before any attempt can exist — and snapshotted onto the
run as `publication_manifest`. From then on the required set comes from the row.

Snapshotting rather than re-reading matters twice: it keeps a MinIO round trip out of the
publisher's hot path, and it stops a later re-render silently redefining what this run was
supposed to do. Only outputs with `final_clip.status == "generated"` are required — a clip
that never rendered is not something to wait for for ever.

If the package cannot be read and the run has no publications to reconstruct from, publication
is **blocked**. An empty manifest would read as "everything required is done", which is the
exact mistake being prevented. Runs that predate the feature derive a `version: 0` manifest
from their own attempts, which reproduces the behaviour they were created under and is
labelled in the data rather than inferred.

### Completion states

| Situation | Completion | Pipeline state |
|---|---|---|
| nothing attempted | `not_started` | `READY_TO_PUBLISH` |
| something queued, uploading, or awaiting a scheduled retry | `in_progress` | `PUBLISHING` |
| some succeeded, rest outstanding, nothing active | `partial` | `READY_TO_PUBLISH` |
| every required item succeeded | `complete` | `PUBLISHED` |
| a required item is `FAILED_FINAL`, `CANCELED`, or out of retries | `blocked` | `READY_TO_PUBLISH` |
| a required item is `UNKNOWN` | `unresolved` | `READY_TO_PUBLISH` |

Releasing a partly published run back to `READY_TO_PUBLISH` is not a failure report — it is how
a run larger than a daily budget makes progress. A `FAILED_RETRYABLE` item counts as *active*
only while a retry is still coming; once its attempt budget is spent nothing will pick it up
again, and leaving the run in `PUBLISHING` would imply otherwise.

| Attempt state | What automation may do |
|---|---|
| `SUCCEEDED` | nothing — never republished |
| `PENDING` / `IN_PROGRESS` | nothing — it is in flight |
| `FAILED_RETRYABLE`, retries left | nothing — the queue owns the retry |
| `FAILED_RETRYABLE`, exhausted | nothing — a human must act |
| `FAILED_FINAL` / `CANCELED` / `UNKNOWN` | nothing — no replacement, ever |
| no attempt | allocate it |

Completion is scoped to one target, and blind to who published: a clip uploaded by an operator
and one uploaded by automation are both external successes. Who decided is a budget question.

### Safe partial allocation

With completion outstanding-aware, the budget no longer has to take a whole run or none of it.
A four-clip run against a cap of two publishes two clips today and the other two tomorrow:

```
day 1   allocate 2   ->  2/4  READY_TO_PUBLISH
day 2   allocate 2   ->  4/4  PUBLISHED
```

Deterministic by `video_index`, so a series publishes first clip first, and successes are never
offered for allocation again. A manual publish of a subset behaves the same way: clips 3 and 4
stay outstanding, and automation finishes what the operator started.

```json
{"completion": {"required": 4, "succeeded": 2, "outstanding": 2,
                "in_flight": 0, "retry_pending": 0, "blocked": 0, "unresolved": 0}}
```

## Publication budget

`AUTOPUBLISH_MAX_PER_DAY` is enforced, not observed. It used to be read once and then spent,
so two replicas ticking together both saw the same remaining figure and both took it:

```
replica A   count -> 2   remaining 1   creates #3
replica B   count -> 2   remaining 1   creates #4     cap breached
```

Allocation is now serialised by a session-scoped PostgreSQL advisory lock, and usage is
**recomputed inside it** before every unit. The authority is the publication rows themselves,
not a counter beside them — a counter is a second truth that drifts from the first the moment
an attempt creation fails after it moved.

The lock is session-scoped rather than `pg_advisory_xact_lock` (which the rest of the codebase
uses) because creating a publication commits several times inside the publish command, and a
transaction-scoped lock would be released by the first of those — leaving the rest of the
allocation unprotected, which is exactly the window being closed.

**One unit is one logical external publication:** one media item, on one target, once.

| Spends a unit | Does not |
|---|---|
| a new automatic `PublishAttempt` row | a retry (`attempt_no + 1` on the same row) |
| each clip of a multi-clip run | a queue redelivery or a provider call |
| | a manual publication (`initiator = manual`, no `budget_date`) |
| | an attempt an operator canceled |

The day is **UTC**, from a `budget_date` column written at creation — never a range over
`created_at`, whose naive default would resolve through the container's timezone and make the
boundary mean different things to replicas in different zones.

**Partial allocation is safe** since PR-PUBLISH-COMPLETE-01. A run takes as much as the
remaining budget allows and keeps the rest outstanding; see *Publication completion* above.
Until then it had to take a whole run or none, because a partly published run was settled to
`PUBLISHED` and its unallocated clips silently dropped.

## Publication spool hygiene

`MinioMediaSource` spools media to `/tmp/clipflow-publish-*.mp4` and deletes it in a `finally`.
`finally` does not run on `SIGKILL`, and a spool is the size of a video. The publisher sweeps
at startup — which is precisely when the leftovers of a dead process are there and nothing is
using them.

Two rules, both load-bearing: the name must match the prefix *and* suffix this codebase writes,
so an unrelated file sharing the directory is never touched; and the file must be older than
`PUBLISH_TEMP_STALE_SEC` (6h), comfortably longer than any upload, so a spool a live publisher
is streaming from right now survives. The second rule is what makes it safe if publishers ever
share a volume — they do not today, but that is a compose edit away from changing.

## Performance metrics and lineage

Measurement, not optimization. Collection records what happened to videos that were already
published, and **nothing it records changes what the system does next** — not discovery
ranking, not the selection score, not relevance or trend, not admission, not clip planning,
not QA, not publication eligibility, not the autopublish policy. There is no feedback loop,
deliberately: a loop built before the data exists would optimise against a guess. This is the
empirical base; what to do with it is a separate decision on separate evidence.

That independence is enforced structurally rather than promised. Nothing in `app/discovery`,
`app/selection`, `app/publishing` or the admission, scheduling and publishing services imports
`app.metrics`, and `test_metrics_package_is_not_imported_by_the_production_path` fails if that
ever changes — a future feedback edge has to show up as an import in a diff.

### What is collected

`videos.list?part=statistics,status` on the YouTube **Data** API: `viewCount`, `likeCount`,
`commentCount`, plus upload and privacy status. Fifty ids per call for one quota unit, so a
backlog of 500 videos costs 10 units and not 500.

The Data API works with `youtube.readonly`, a scope every connected target already granted
when it was connected. The **Analytics** API offers far more — watch time, retention, traffic
sources — but needs `yt-analytics.readonly`: a new scope, meaning every existing target has to
be disconnected and reconnected. That is a real cost to pay before anyone has looked at a
single view count, so it is not paid here. It is the obvious next step when the richer figures
are actually wanted.

Collection is grouped by `PublishTarget` because the OAuth credential is per target. A batch
only ever contains videos from the channel whose token is being used.

### The snapshot table

`video_performance_snapshots` is append-only. A later collection inserts a new row; it never
updates an earlier one.

```
10:00  views=100
12:00  views=180
18:00  views=410
```

Overwriting would leave the system knowing a video has 410 views and nothing about how it got
there, and the shape of that curve is the entire reason to collect anything.

Four rules the schema enforces or the ingestion honours:

- **Absolute counters, not deltas.** What the provider reported, as reported. A delta is
  derivable from two snapshots; a missed collection makes a stored delta wrong for ever while
  leaving an absolute counter merely sparse.
- **Counters may go down.** YouTube removes spam views and deleted comments, so `new >= old`
  is not an invariant and is not enforced. A decrease is a valid observation.
- **NULL is not zero.** YouTube omits `likeCount` when the owner hides likes and
  `commentCount` when comments are disabled. Zero means "observed, and it was zero"; NULL
  means "not disclosed". The distinction survives all the way to the API response.
- **A video that vanishes is classified, never zeroed.** Deleted, made private, region-blocked
  — the API does not say which, so the row records `availability = not_returned` with NULL
  counters. Writing `views=0` would look like a catastrophic collapse on any chart.

Counters are `BIGINT`: a successful video exceeds 2³¹ views, and finding that out through an
overflow is not the way.

### Cadence

Newer videos move faster, so they are watched more closely:

| Age of the video | Collected every |
| --- | --- |
| under 24h | `METRICS_INTERVAL_FRESH_HOURS` (1h) |
| 1–7 days | `METRICS_INTERVAL_RECENT_HOURS` (6h) |
| older | `METRICS_INTERVAL_MATURE_HOURS` (24h) |

Past `METRICS_TRACKING_DAYS` (30) a publication drops out entirely: its series is history
rather than signal, and polling it would spend the quota fresh videos need.

A fixed table rather than an adaptive scheduler. Quota is the binding constraint and a simple
rule is one an operator can predict; an adaptive one would be a second system to reason about
before anyone has looked at the first day of data.

### Idempotency

Each row carries a `capture_slot` — the UTC hour it belongs to, stored as `2026-09-04T13` so
the rounding rule is visible in the data rather than implied by a query — and
`(publish_attempt_id, capture_slot)` is unique.

Two layers, for two different races. The cadence stops a *second run* from spending quota an
hour early. The unique constraint stops a *second row*: two replicas can both evaluate "is
this due?" before either has written, and a check-then-insert walks straight through that. A
`pg_try_advisory_lock` sits in front of the whole round so the losing replica skips rather
than duplicating the work — try, never wait, because the loser has nothing useful to do.

### Failure is never a production gate

If Google is down, a token expired, or the collector raises outright, discovery still runs,
selection still ranks, admission still admits and the publisher still publishes. Collection
rides the automation loop but runs strictly *after* the production tick, inside its own
`try/except`, on its own much slower cadence (`METRICS_POLL_INTERVAL_SEC`, 15m).

A rejected credential reuses the existing vocabulary: the target goes to
`reconnect_required`, exactly as a failed publication would leave it. There is no second
notion of a broken credential for the publisher and the collector to disagree about. A
disconnected target is reported as `target_unavailable` and skipped — one broken channel must
not cost every other channel its collection.

The operational signal `metrics_collection_stale` is the only **LOW** severity signal in the
system, and LOW signals deliberately do not move the overall status. Analytics running late is
an inconvenience, never an incident, and a health status that cries wolf over it is one nobody
reads.

### Lineage

Each clip of a run is its own video with its own audience, so snapshots hang off
`PublishAttempt` rather than `PipelineJob` — aggregating at ingestion would destroy exactly
the comparison the data exists to make: which cut of the same match did better.

That gives the full chain, end to end:

```
DiscoverySource -> VideoCandidate -> ContentTopic -> PipelineJob -> PublishAttempt -> YouTube video
```

`complete: true` means every link is a real foreign key. A publication whose job has no
candidate reports an *unknown* origin and `complete: false`. Matching on title, or on the
nearest candidate by time, would manufacture provenance that reads as authoritative and is a
guess.

### Endpoints

All admin-only. None of them returns a refresh token, an access token, an encrypted
credential, an upload session URI, or a raw provider error body — the read models are built
from columns chosen for that reason, not filtered afterwards.

```
GET  /admin/published-videos/{attempt_id}/performance   the temporal series
GET  /admin/published-videos/{attempt_id}/lineage       source -> published video
GET  /admin/metrics/status                              enabled, tracked, due, last capture
POST /admin/metrics/youtube/run?dry_run=true            collect now (dry run by default)
```

`dry_run` defaults to **true**. The safe reading of "run this" is "show me what it would do",
and a real run spends the channel's YouTube quota.

Collection is off by default (`METRICS_COLLECTION_ENABLED=false`), like every other autonomous
behaviour here.

## Evaluation dataset

Snapshots alone are not comparable. A hundred views one hour after publication and a hundred
views two weeks later are not the same fact, and the collector records whichever moments it
happened to be awake for. This layer asks every publication the *same* question, so two videos
can be put beside each other without the comparison secretly being about their ages.

**Evaluation, not optimization.** Nothing here changes discovery, relevance, trend, selection,
admission, clip planning, QA, publication eligibility, the autopublish policy or the
publication budget. It reads; it never writes. As with ingestion, that is enforced by the
import graph — no production module imports `app.evaluation`, and
`test_evaluation_package_is_not_imported_by_the_production_path` fails if one ever does.

### The unit is one published video

One `PublishAttempt` that succeeded is one row. Not one run: a run can render four clips,
upload all four, and get four different audiences. Aggregating at the run would average away
the only comparison the data exists to support — which cut of the same match did better — and
no later analysis could recover it.

### Canonical windows

Five, one per regime a short video actually passes through:

| Window | Tolerance | Rule |
| --- | ---: | --- |
| 1h | 1h | earliest measuring snapshot with age in [1h, 2h] |
| 6h | 2h | earliest measuring snapshot with age in [6h, 8h] |
| 24h | 8h | earliest measuring snapshot with age in [24h, 32h] |
| 72h | 12h | earliest measuring snapshot with age in [72h, 84h] |
| 7d | 30h | earliest measuring snapshot with age in [168h, 198h] |

The tolerances are **measured**, not assumed. Replaying the shipped ingestion cadence (hourly
under 24h, 6-hourly to 7d, daily after) against a 15-minute collection tick gives the earliest
observation at or after each target:

| Window | First observation | Lag |
| --- | ---: | ---: |
| 1h | 1h00m | 0h00m |
| 6h | 6h00m | 0h00m |
| 24h | 29h00m | 5h00m |
| 72h | 77h00m | 5h00m |
| 7d | 191h00m | 23h00m |

The 24h, 72h and 7d lags are not noise. The collection interval widens *at* those ages, so the
schedule steps straight over the boundary it was asked about: a 24-hour-old video becomes due
again six hours later, not one, and nothing is captured between 24h and 29h. Tolerances are
therefore the measured need plus real margin. `views_24h` honestly means "views at the first
observation from 24h onward", and every row carries the lag that says how much later that was.

### Four rules the resolver keeps

- **At or after the target.** An observation at 23h cannot answer "what did 24h look like",
  however close it feels — accepting it would make the column mean 23 hours of exposure for
  some videos and 24 for others.
- **Bounded, never nearest-available.** Without an upper bound a 24h window would happily
  answer with a snapshot from day nine.
- **No interpolation.** View growth is not linear, so a straight line between 23h and 25h is a
  fabrication indistinguishable from a measurement once written down. If no acceptable
  observation exists, the answer is unavailable.
- **A real measurement wins.** One `not_returned` blip at 24h05 does not discard a good
  observation at 24h30.

### Availability, not NULL

| State | Meaning |
| --- | --- |
| `available` | measured, inside the acceptance interval |
| `not_mature` | the interval has not closed yet as of the cut-off — an observation may still arrive |
| `missing_snapshot` | the interval closed and nothing fell inside it — a statement about the collector |
| `video_not_returned` | a snapshot exists but the provider did not return the video |

"Too early to know" and "we should have known and did not" call for opposite responses, so they
are never collapsed into one another. Maturity is judged against the dataset's `as_of` rather
than the wall clock, or the same dataset would answer differently every time it was rebuilt.

### Reproducibility, without a second copy

Every build takes an `as_of`: snapshots captured after it are invisible, in SQL and again in
the resolver. That is the whole look-ahead guard — a dataset rebuilt next week, with a week of
new observations in the table, returns identical rows.

Because of it, nothing is materialized. Snapshots are append-only and never backfilled, so
`(as_of, semantic version, window policy, filters)` already determines the output exactly; a
stored copy would only add a second truth that can drift from the series it came from. What is
emitted instead is a manifest, and a `dataset_id` that is a digest of those same inputs — so
the same request names the same dataset, and changing any input changes the id. Asking for an
id that does not match the parameters returns 404 rather than quietly handing back something
else.

### Decision context, publication context, outcomes

Three groups, kept apart structurally and prefixed `dc_` / `pub_` / `out_` once flattened.

`dc_` is what was knowable *before* the video existed, read from the provenance admission
froze onto the job — the selection score the decision actually saw, not whatever the candidate
says today. `out_` is what happened afterwards. `pub_` is neither: privacy and initiator were
fixed at upload, and they are confounders to condition on rather than features or results.

The separation is not cosmetic. The obvious future use of this dataset is to learn something
from it, and a table that mixes the two invites a model trained on `views_24h` to predict
`views_24h`. Leakage now has to be introduced on purpose.

### Derived fields

Absolute counters are preserved as observed, including decreases — YouTube removes spam views,
so monotonicity is not an invariant. On top of them, only exact arithmetic:

- `views_per_hour_{1h,6h,24h}` — divided by the observation's **actual** age, not the nominal
  window. 1,100 views seen at 29h is 37.9/hour, not the 45.8 that dividing by 24 would claim.
- `likes_per_view_24h`, `comments_per_view_24h`.

A ratio is NULL when its numerator was not disclosed (a hidden like count is unknown, and 0.0
would report the video as having no engagement) and NULL when views are zero ("0 likes out of
0 views" is not 0% engagement; it is a question nobody has asked yet).

There is deliberately **no** viral score, quality score, performance score or ranking. Each
would need an empirical definition nobody has yet, and once such a column exists somebody will
sort on it.

### Data quality

Every considered publication is either a row or a counted exclusion — `considered = included +
excluded`, with reasons (`missing_lineage`, `missing_published_at`, `missing_external_id`).
Coverage is reported per window as available over mature. It measures the **collector**, not
the content: low 24h coverage means the loop was not running, never that the videos did badly.

### Endpoints

All admin-only, all read-only, and none of them calls YouTube — ingestion talks to the
provider, evaluation talks to the database.

```
GET  /admin/published-videos/{attempt_id}/evaluation      one publication, with its trace
POST /admin/metrics/evaluation-datasets?dry_run=true      build/preview: manifest, summary, quality
GET  /admin/metrics/evaluation-datasets/{dataset_id}      paginated rows, id must match params
GET  /admin/metrics/evaluation-datasets/{id}/export.csv   the whole dataset, streamed
GET  /admin/metrics/evaluation-schema                     window policy and column contract
```

`dry_run` defaults to true, which here means "tell me what this dataset would contain" — that
is the question worth asking first, because a dataset whose 24h coverage is 30% is not one to
start analysing. The CSV has declared, versioned columns (NULL is the empty field) and carries
its manifest in `X-Dataset-*` headers.

### What this dataset cannot tell you

It is observational, and it is small. Stated plainly, because the temptation to read more into
it than it supports will only grow as it fills:

- **Views are exposure-dependent.** They measure distribution as much as content.
- **Privacy is a confounder.** A `private` upload has no public distribution at all, so private
  and public videos must never be compared as though they had the same exposure. Privacy is
  preserved as a dimension and deliberately not corrected for.
- **Topics and source channels differ**, in audience size and in baseline interest.
- **Canonical windows reduce age bias; they do not eliminate it.** A 24h observation is really
  a first-observation-from-24h-onward, and its lag varies.
- **Channel-size normalization is not possible yet.** Nothing has ever recorded a subscriber
  count, and using today's would leak future information into a past publication.
- **No causal claim is available.** Even a strong correlation between selection score and views
  would not show that the score *causes* views: the same score also decides what gets produced,
  when, and on which topic. Historical performance does not imply future performance.

## Job state

A run's state is the result of a validated transition, never an inference from which objects
happen to exist in storage.

```
producer -> PipelineJob (QUEUED) -> Redis payload {job_id, pipeline_job_id}
                                          |
                                       worker claims
                                          |
                              stage reports -> internal API
                                          |
                              PipelineStateMachine -> PostgreSQL
                                          |
                                   PipelineEvent -> SSE
```

**Three authorities, kept apart.** Redis owns possession of the message — a claim, a retry
and a dead-letter are queue facts. The worker owns *facts*: the step it is executing, that it
failed, that it finished. The API owns *state*: it maps a step to a lifecycle state, checks
the transition and persists it. The worker never writes to the database and never names a
state, so it cannot route around the rules.

**States.** `QUEUED → DOWNLOADING → DOWNLOADED → TRANSCRIBING → TRANSCRIBED → ANALYZING →
PROMPT_BUILDING → WAITING_AI → AI_COMPLETED → RENDERING → RENDERED`, then either
`READY_TO_PUBLISH` or `REVIEW_REQUIRED` depending on PR-QA-01's publication verdict.
`FAILED` and `CANCELED` are reachable from anywhere in flight. `PUBLISHING`/`PUBLISHED`
exist for a publisher that does not: **`READY_TO_PUBLISH` is not `PUBLISHED`.**

The `…ED` states are checkpoints, and no worker step means "transcribed" — it just starts
chunking. So a run may skip forward to any later production state; what is refused is moving
*backwards*, which is the direction that actually corrupts a timeline.

**Delivery is at-least-once, transitions are idempotent.** A worker may report the same step
twice (HTTP retry, restart, duplicate delivery), and several steps map to one state — seven
analysis steps mean one `ANALYZING` transition. Each report is classified:

| Outcome | Meaning |
|---|---|
| `applied` | the run moved |
| `duplicate` | already in that state — no-op, no event |
| `regression_ignored` | the report is stale; the run is left alone |
| `not_mapped` | a real step that does not move the lifecycle (recorded as an event) |
| `invalid` | not an allowed edge |

Concurrent reports are serialised with `SELECT … FOR UPDATE`, so a late report re-reads the
state the earlier one committed instead of overwriting it.

**Retries reuse the run.** One `PipelineJob` spans every attempt; `retry_count` says which is
current. Re-queueing is a *command* from the queue runner, not a progress report — the one
legitimate backwards move, and unreachable from a stage report because no step maps to
`QUEUED`.

**Identity.** Three ids, not interchangeable:

| Id | Identifies | Notes |
|---|---|---|
| `worker_job_id` | where the bytes live (`jobs/<id>/…`, queue payload) | the `ClipJob.id` for API jobs; a bare UUID for Telegram ones |
| `PipelineJob.id` | the run | created by every producer before enqueueing |
| `ClipJob.id` | the billing-coupled customer job | absent for Telegram runs — which is why the run cannot hang off it |

**Reads do not touch object storage.** `GET /jobs/{id}/events` and the SSE stream read the
database. Artifact reconciliation still exists, but as an explicit operation
(`POST /internal/jobs/{id}/sync-artifacts`) and as the fallback for jobs enqueued before runs
existed. Those are labelled `state_source: "legacy_artifact_inference"`; authoritative ones
are `"pipeline"`. The two are never mixed silently, and no history is fabricated for old
jobs.

## Two quality gates

A job passes through two independent checks that answer different questions.

```
cut plan -> cuts -> [ Source QA ] -> render -> [ Final Media QA ] -> publication eligibility
```

**Source QA** (`worker/app/video/qa.py`, `qa_report.json`, `qa_scope: "source_cut"`) judges
the editing: were the right ranges chosen, do they land on speaker boundaries, is the post
metadata there.

**Final Media QA** (`worker/app/video/final_media_qa.py`, `final_qa_report.json`,
`qa_scope: "final_output"`) judges the MP4 that would actually be published. Between the two,
the renderer changes playback speed, prepends a cold open, applies transitions, concatenates,
mixes a soundtrack and burns in subtitles — six transformations, any of which can produce a
file that is silent, black, truncated, the wrong shape or undecodable while every source-cut
check still passes.

The file is measured, not assumed: one `ffprobe` for the container, and one
`ffmpeg -f null -` decode pass carrying `blackdetect`, `freezedetect`, `silencedetect` and
`volumedetect` together. That pass is the decode-integrity check, so the analysis rides along
for free. Cost on a real render: **0.027x realtime** — 134s of media analysed in 3.7s.

Each check reports separately, so a decision can be explained:

```json
{
  "status": "blocked",
  "checks": {
    "container_valid":  {"status": "pass"},
    "duration":         {"status": "pass"},
    "dimensions":       {"status": "fail", "code": "wrong_aspect_ratio"},
    "audio_silence":    {"status": "fail", "code": "audio_fully_silent"},
    "subtitle_timing":  {"status": "pass"}
  },
  "reasons": ["audio_fully_silent", "wrong_aspect_ratio"],
  "retry_classification": "retry_will_not_help"
}
```

| Check | Blocks | Needs review |
|---|---|---|
| container / streams / duration | missing, empty, unopenable, no video, no audio, duration ≤ 0 | — |
| decode integrity | decode errors, decode timeout | — |
| duration vs plan | beyond 4x tolerance | beyond tolerance |
| dimensions | ratio off contract by more than 0.02 | — |
| picture | ≥60% black | ≥25% black, freeze > 4s |
| audio | fully silent, ≥50% silent, flat-topped waveform | longest silence > 8s, peak at 0 dBFS, mean < −45 dB |
| subtitles | — | out of bounds, negative, out of order, empty |
| transitions | — | fade longer than its clip |

**Expected duration is modelled, not guessed.** `sum(source_cut_duration)` is the wrong
baseline: the renderer divides each clip by the playback speed and prepends a cold open.
Measured on a real render, the naive comparison is off by 2.3s while the model is off by
0.19s — a tolerance wide enough to absorb the naive error would let a truncated render
through. Transitions are `fade`/`afade` *inside* a clip rather than crossfades, so they are
duration-neutral; the concat, the soundtrack mix and the subtitle burn are too.

**Nothing technical is laundered by a good score.** `auto_ready` requires both layers.
A blocked final file blocks the job whatever the editorial score, and — the invariant a
publisher will depend on — a technical failure never yields publication eligibility. Absence
of a verdict is not a pass either: if Final Media QA did not run, the technical gate reads
`unmeasurable` and the job needs a human. There is no publisher yet;
`publication_eligibility.publisher_available` is `false`.

Run the gate's own evaluation (it generates real MP4 fixtures with ffmpeg, then deletes them):

```bash
python -m evaluation --final-qa
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
