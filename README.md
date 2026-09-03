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
 PublishAttempt    committed BEFORE any byte leaves, and claimed atomically
      |
 YouTube Data API v3, resumable upload, streamed in 256 KiB chunks
      |
  external_id → PUBLISHED
```

**Why the ordering is the reverse of admission's.** Admission commits a row then enqueues,
because a lost message is recoverable. Publishing commits the attempt *before* the first byte
and writes the external id *after* the provider confirms, because the thing that must never
happen is a video existing that this system has no record of.

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

# dry run is the default
curl -X POST localhost:8010/admin/pipeline-jobs/{id}/publish -d '{"target_id": "..."}'
curl -X POST localhost:8010/admin/pipeline-jobs/{id}/publish      -d '{"target_id": "...", "dry_run": false, "privacy": "private"}'

curl localhost:8010/admin/publish-attempts/unresolved                # the operator's queue
curl -X POST localhost:8010/admin/publish-attempts/{id}/reconcile    # ask the session
curl -X POST localhost:8010/admin/publish-attempts/{id}/resolve -d '{"external_id": "..."}'
```

Every route is admin-only on the existing RBAC. The OAuth callback is the one exception and
cannot be otherwise — the browser arriving from Google carries Google's session, not ours;
its authorisation is the single-use `state`.

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
