# ClipFlow SaaS V2 Product Plan

## Objective

Define the V2 SaaS structure for the ClipFlow product with two commercial lines:

- AI-assisted video cutting
- AI-generated social video scripting and posting package

This document is written as a practical operating plan for product, architecture, billing, and capacity planning.

## Product Vision

ClipFlow V2 should evolve from a pipeline-oriented internal tool into a productized SaaS with two clear user outcomes:

1. Turn existing long videos into ready-to-publish cuts.
2. Generate a complete social video package from an idea, topic, or brief.

The product should feel like an editorial copilot, not just a clipper.

## Core Product Lines

### 1. Cuts

The cuts product transforms uploaded or linked videos into finished editorial assets.

Primary deliverables:

- selected cuts
- strong hooks
- final rendered videos
- titles
- descriptions
- hashtags
- thumbnail guidance
- posting metadata

Commercial SKUs:

- `3 short independent cuts`: R$ 3,99
- `1 long-form cut`: R$ 2,99
- `2 serialized long-form cuts`: R$ 3,99

### 2. Script Studio

The new Script Studio product generates a complete package for recording and publishing a social video.

Primary deliverables:

- full script
- spoken lines
- hook options
- scene-by-scene structure
- thumbnail concepts
- titles
- descriptions
- hashtags
- posting plan
- recording instructions
- editing instructions

Commercial SKU:

- `1 complete social video script package`: R$ 0,99

## V2 Commercial Model

### Suggested Credit Model

To keep checkout simple, V2 should operate with micro-credit packs.

Recommended mapping:

- `1 credit` = `1 roteiro completo`
- `3 credits` = `1 corte longo`
- `4 credits` = `3 cortes curtos independentes`
- `4 credits` = `2 cortes longos serializados`

### Suggested Top-Up Packs

These packs are simple, easy to explain, and preserve margin:

- `R$ 1,99` -> `2 credits`
- `R$ 4,99` -> `5 credits`
- `R$ 9,99` -> `11 credits`
- `R$ 19,99` -> `24 credits`
- `R$ 39,99` -> `52 credits`

### Why Credits Instead Of Only Fixed Plans

Credits fit this product better because:

- usage is irregular
- users may want scripts without cuts
- users may want cuts without scripts
- the compute cost per job varies a lot
- credits simplify future bundles and promotions

## Product Experience

### Main User Flows

#### A. Cut Flow

1. User creates a job from a URL or upload.
2. User selects mode:
   - short
   - short serie
   - long
3. User waits for transcript and editorial context.
4. User optionally approves or edits the AI response.
5. User receives final clips and posting metadata.

#### B. Script Flow

1. User opens Script Studio.
2. User provides:
   - topic
   - platform
   - goal
   - target audience
   - tone
   - language
   - duration target
3. User receives a full publishing package.
4. User can export or convert that script into a future recording/cut job.

## New Feature: Script Studio

### What It Should Generate

For each script request, the system should generate:

- `script_title`
- `core_angle`
- `hook_options`
- `final_hook`
- `full_script`
- `spoken_lines`
- `scene_plan`
- `thumbnail_options`
- `title_options`
- `description_options`
- `hashtags`
- `cta_options`
- `posting_plan`
- `recording_instructions`
- `editing_instructions`

### Example Output Contract

```json
{
  "platform": "instagram_reels",
  "language": "pt-BR",
  "target_duration_sec": 45,
  "core_angle": "por que vídeos curtos falham no começo",
  "hook_options": [
    "Se o seu vídeo começa morno, ele já morreu.",
    "O erro do seu hook acontece antes da primeira frase."
  ],
  "final_hook": "Se o seu vídeo começa morno, ele já morreu.",
  "title_options": [
    "O erro que mata seu vídeo em 2 segundos",
    "Seu hook está matando sua retenção"
  ],
  "thumbnail_options": [
    "Texto curto: 'Seu hook morreu'",
    "Texto curto: '2 segundos fatais'"
  ],
  "full_script": "...",
  "spoken_lines": ["...", "..."],
  "scene_plan": [
    {
      "scene_index": 1,
      "goal": "hook",
      "visual_direction": "close-up",
      "spoken_text": "..."
    }
  ],
  "description_options": [
    "...",
    "..."
  ],
  "hashtags": ["#marketing", "#reels", "#conteudo"],
  "posting_plan": {
    "platform": "instagram_reels",
    "timezone": "America/Recife",
    "best_time_windows": ["11:30-13:00", "18:00-20:30"],
    "best_weekdays": ["terça", "quarta", "quinta"],
    "reasoning": "..."
  },
  "recording_instructions": ["..."],
  "editing_instructions": ["..."]
}
```

## Specialized Agent Design

The scripting feature should not be a single generic LLM prompt.

It should be designed as a specialized agent stack.

### Agent 1. Content Strategist

Responsibility:

- define the angle
- define audience fit
- define platform fit
- define retention strategy

Outputs:

- `core_angle`
- `content_goal`
- `audience_fit`
- `hook_strategy`

### Agent 2. Script Writer

Responsibility:

- write the spoken narrative
- create the hook
- structure payoff and CTA

Outputs:

- `full_script`
- `spoken_lines`
- `hook_options`
- `final_hook`

### Agent 3. Thumbnail And Packaging Agent

Responsibility:

- create thumbnail directions
- create title options
- create description options
- create hashtag options

Outputs:

- `thumbnail_options`
- `title_options`
- `description_options`
- `hashtags`

### Agent 4. Posting Planner

Responsibility:

- suggest posting windows
- suggest cadence
- suggest platform-specific packaging

Outputs:

- `posting_plan`
- `cta_options`

### Agent 5. Recording And Editing Coach

Responsibility:

- explain how to record
- explain how to edit
- adapt delivery to camera and creator format

Outputs:

- `recording_instructions`
- `editing_instructions`

## SaaS V2 Functional Modules

### 1. Identity And Access

- login
- role-based permissions
- tenant support
- session management
- audit logs

### 2. Billing And Wallet

- credit wallet
- product catalog
- checkout
- credit deduction
- refund rules
- ledger

### 3. Job Orchestration

- create job
- enqueue
- track status
- retry safely
- artifact sync

### 4. Media Pipeline

- download/upload ingest
- transcript
- speaker segmentation
- span catalog
- hook candidate generation
- AI-assisted selection
- render
- delivery package

### 5. Script Pipeline

- brief intake
- specialized agent orchestration
- generation
- validation
- export

### 6. Editorial Review

- human review queue
- approve/reject/adjust clips
- approve script package
- notes and revision history

### 7. Publishing Intelligence

- metadata package
- best time suggestions
- title variants
- thumbnail variants
- platform-specific packaging

### 8. Analytics

- jobs created
- jobs completed
- average time to completion
- credit consumption
- asset download rates
- script usage vs cut usage

## Multi-Tenant SaaS Structure

### Tenant-Level Configuration

Each tenant should eventually control:

- default language
- default clip modes
- subtitle style
- hook aggressiveness
- posting timezone
- brand voice
- thumbnail style
- default soundtrack preferences

### User-Level Profile

Each user should store:

- preferred language
- default timezone
- preferred platform
- favorite posting windows
- brand settings

## Data Model Additions For V2

### New Entities

- `wallet_accounts`
- `wallet_ledger_entries`
- `catalog_products`
- `catalog_credit_packs`
- `script_jobs`
- `script_outputs`
- `tenant_profiles`
- `brand_profiles`
- `posting_profiles`

### Current Job Model Direction

The system should move toward:

- `jobs` independent from billing products
- billing linked through wallet transactions or purchases
- internal operation never blocked by catalog state

## Suggested V2 API Surface

### Cuts

- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{id}`
- `GET /jobs/{id}/editorial-context`
- `POST /jobs/{id}/submit-ai-response`
- `GET /jobs/{id}/delivery-package`

### Script Studio

- `POST /script-jobs`
- `GET /script-jobs`
- `GET /script-jobs/{id}`
- `GET /script-jobs/{id}/output`

### Billing

- `GET /catalog/products`
- `GET /catalog/credit-packs`
- `POST /wallet/top-up`
- `GET /wallet`
- `GET /wallet/ledger`

## UX Recommendations

### Studio IA

The Studio should expose two clear entry points:

- `Novo Corte`
- `Novo Roteiro`

Do not mix these into one confusing generic “new job” concept.

### Job Detail

Keep the current direction:

- operation
- editorial
- review
- logs

For Script Studio, add:

- brief
- strategy
- script
- packaging
- posting plan

## V2 Architecture

### Services

- `clipflow-studio`: frontend SaaS UI
- `clipflow-api`: auth, billing, wallet, jobs, script jobs
- `control-plane`: Telegram/internal entrypoint and orchestration support
- `worker`: media pipeline
- `script-worker` or `agent-runner`: script generation pipeline
- `postgres`: relational data
- `redis` or managed queue layer
- object storage for artifacts

### Recommended Separation Of Worker Roles

Do not keep all media responsibilities in one generic worker pool forever.

Split by responsibility:

- `orchestrator workers`
- `cpu media workers`
- `gpu media workers`
- `script agent workers`

This will matter a lot for cost and scaling.

## Pricing Summary

### End-User Prices

- `Roteiro completo`: R$ 0,99
- `3 cortes curtos independentes`: R$ 3,99
- `1 corte longo`: R$ 2,99
- `2 cortes longos serializados`: R$ 3,99

### Pack Strategy

Credits keep billing flexible while preserving simple customer communication.

Example public messaging:

- “Compre créditos e use quando quiser”
- “1 crédito para roteiro”
- “3 ou 4 créditos para pacotes de cortes”

## Capacity Report For Peak 200 Users Per Minute

This section is an engineering planning estimate, not a benchmark result.

### Planning Assumptions

Assumptions used in this estimate:

- peak ingress: `200 users/minute`
- burst duration considered: `15 minutes`
- workload mix:
  - `35%` script jobs
  - `35%` 3 short cuts
  - `15%` 1 long cut
  - `15%` 2 serialized long cuts
- cuts are asynchronous jobs
- script jobs are mostly LLM-bound
- media jobs are queue-based and do not complete synchronously at the same rate as ingress

### Peak Ingress Breakdown

At 200 users/minute:

- `70 script jobs/min`
- `70 short-cut jobs/min`
- `30 single-long jobs/min`
- `30 serialized-long jobs/min`

For a 15-minute burst:

- `1,050 script jobs`
- `1,950 media jobs`

### Important Operational Reality

The API can admit 200 users/minute fairly easily.

The expensive part is not request admission.
The expensive part is draining the media queue with acceptable SLA.

This means V2 must be designed as:

- fast admission
- durable queueing
- asynchronous processing
- elastic worker pools

### Recommended Baseline Platform Capacity

For a professional V2 SaaS, the baseline stack should start around:

#### Web/API Layer

- `clipflow-studio`: CDN + static hosting
- `clipflow-api`: `3 replicas`
  - `2 vCPU`
  - `4 GB RAM`
- `control-plane/internal API`: `2 replicas`
  - `1 vCPU`
  - `2 GB RAM`

This is enough to absorb 200 users/minute comfortably for control-plane and HTTP ingress, assuming jobs are queued.

#### Database

Managed PostgreSQL starting point:

- `4 to 8 vCPU`
- `16 to 32 GB RAM`
- HA enabled
- automated backups

#### Queue / Messaging

Managed queueing is strongly recommended.

Starting point:

- one durable queue for media jobs
- one queue for script jobs
- one retry/dead-letter path

#### Cache / Rate Control

- managed Redis or equivalent
- `2 to 4 GB RAM` to start

#### Object Storage

Start with:

- `5 to 10 TB` planned artifact capacity
- lifecycle rules for cold storage
- separate buckets/containers for:
  - inputs
  - transcripts
  - prompts
  - final renders
  - delivery packages

#### CPU Worker Pool

For download, preprocessing, transcript stitching, packaging and utility tasks:

- `12 to 20 replicas`
- each with `4 vCPU`
- each with `8 GB RAM`

#### Script Agent Worker Pool

For the new script feature:

- `6 to 10 replicas`
- each with `2 vCPU`
- each with `4 GB RAM`

Most script load should scale with LLM API concurrency, not with your own GPU cluster.

#### GPU Worker Pool

For ASR-heavy and render-heavy media processing:

- baseline: `8 GPU workers`
- autoscale target: `24 to 32 GPU workers`

Recommended GPU class:

- 24 GB class inference/render GPUs such as `L4` or equivalent

This GPU choice is an engineering inference based on the current media pipeline profile and should be validated with real benchmark data.

### If You Truly Need To Drain Peak Burst Fast

If the business requirement is not just to accept the burst, but to drain the 15-minute peak quickly:

- for `~2 hour drain target`: plan roughly `48 to 72 GPU workers`
- for `~1 hour drain target`: plan roughly `96 to 144 GPU workers`

These numbers are not safe to treat as final budget numbers.
They are directional planning figures based on the current V2 architecture and mixed workload assumptions.

The correct next step before committing spend is:

1. benchmark each SKU separately
2. measure average GPU-minutes and CPU-minutes per SKU
3. size autoscaling from measured queue drain rate

## Cloud Recommendations

### Recommended First Choice: Google Cloud

Best fit when the product priority is:

- Kubernetes + GPU operations
- managed messaging
- managed PostgreSQL
- object storage
- fast iteration with a modern managed stack

Suggested stack:

- `GKE` for Kubernetes and GPU workloads
- `Pub/Sub` for durable messaging
- `Cloud SQL for PostgreSQL`
- `Cloud Storage`

Why this is strong:

- GKE has current GPU support guidance
- Pub/Sub is fully managed and built for decoupled asynchronous systems
- Cloud SQL reduces database operations overhead
- Cloud Storage is a strong fit for artifact-heavy pipelines

### Strong Alternative: AWS

Best fit when the product priority is:

- broad ecosystem maturity
- Kubernetes familiarity
- enterprise multi-account patterns
- operational flexibility

Suggested stack:

- `EKS`
- `SQS`
- `RDS for PostgreSQL`
- `S3`

Why this is strong:

- EKS managed node groups support GPU-enabled nodes
- SQS is a solid fit for queue-oriented orchestration
- RDS for PostgreSQL is mature and operationally predictable
- S3 is an excellent fit for large artifact pipelines

### Enterprise Alternative: Azure

Best fit when the product priority is:

- Microsoft enterprise environment
- Azure AD / Microsoft identity ecosystem
- enterprise procurement alignment

Suggested stack:

- `AKS`
- `Service Bus`
- `Azure Database for PostgreSQL`
- `Azure Blob Storage`

Why this is strong:

- AKS supports GPU-enabled Linux node pools
- Service Bus is a fully managed enterprise broker
- Azure Database for PostgreSQL is managed and production-oriented
- Blob Storage is appropriate for large unstructured media artifacts

## Recommendation Summary

### Best Product Strategy

- keep cuts as the operational core
- add Script Studio as the next monetizable layer
- use credits instead of only plans
- separate billing from job execution

### Best Technical Strategy

- queue-first asynchronous architecture
- split CPU, GPU and script workers
- treat script generation as a dedicated product line
- do not force billing catalog state to block internal operation

### Best Cloud Strategy

- `GCP first` if speed and managed platform simplicity matter most
- `AWS second` if ecosystem breadth and ops familiarity matter most
- `Azure third` if enterprise Microsoft alignment matters most

## Next Execution Steps

1. formalize V2 catalog and credit rules
2. create `script_jobs` and `wallet` data model
3. add Script Studio UI and API
4. split workers by responsibility
5. benchmark each SKU
6. size autoscaling using real drain-rate data
7. choose cloud based on GPU availability in your target region
