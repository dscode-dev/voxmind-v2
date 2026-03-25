# VoxMind / ClipFlow no Docker Compose

Esta stack roda sem Kubernetes e foi pensada para um servidor Linux com Docker, Docker Compose e GPU NVIDIA.

## Serviços

- `voxmind-worker`
- `voxmind-control-plane`
- `clipflow-api`
- `clipflow-studio`
- `redis`
- `clipflow-postgres`
- `minio`

## Pré-requisitos

1. Docker Engine
2. Docker Compose Plugin
3. NVIDIA Container Toolkit
4. Driver NVIDIA funcionando no host

Teste rápido da GPU:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

## Setup

1. Copie o arquivo de exemplo:

```bash
cp .env.compose.example .env.compose
```

2. Edite:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DIARIZATION_HF_TOKEN`
- `JWT_SECRET`
- `INTERNAL_API_TOKEN`
- senhas de Postgres e MinIO

3. Opcional: adicione beds de trilha em:

```text
worker/assets/soundtracks/
```

Nomes suportados:
- `mystery_tension_bed.mp3`
- `finance_tension_bed.mp3`
- `political_tension_bed.mp3`
- `generic_bed.mp3`

## Subida

```bash
docker compose --env-file .env.compose up -d --build
```

## Endpoints

- ClipFlow API: `http://localhost:8010`
- ClipFlow Studio: `http://localhost:3000`
- Control Plane health: `http://localhost:8000/health`
- MinIO API: `http://localhost:9000`
- MinIO Console: `http://localhost:9001`

## Worker com GPU

Config padrão recomendada:

- `ASR_DEVICE=cuda`
- `ASR_COMPUTE_TYPE=float16`
- `ASR_MODEL_SIZE=large-v3`
- `ASR_PARALLEL_WORKERS=4`
- `ASR_CPU_THREADS=12`

A diarização continua habilitada, mas o `SpeakerDiarizer` segue forçando CPU no pipeline atual por estabilidade.

## Logs úteis

```bash
docker compose --env-file .env.compose logs -f voxmind-worker
docker compose --env-file .env.compose logs -f voxmind-control-plane
docker compose --env-file .env.compose logs -f clipflow-api
```

## Observações

- `clipflow-api` executa `alembic upgrade head` ao subir.
- `minio-init` cria os buckets automaticamente.
- o worker usa volumes persistentes para `/work` e `/cache`.
- o pipeline final gera:
  - `render_plan.json`
  - `delivery_package.json`
  - `publish_package.json`
  - `final_reel.mp4`
  - `final_reel.srt`
