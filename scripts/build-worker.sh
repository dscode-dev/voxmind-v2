#!/usr/bin/env bash
#
# Build the ClipFlow GPU worker images explicitly, in the required order.
#
# `docker compose build voxmind-worker` already does this on its own (the base is wired in
# through `additional_contexts: service:`). This script exists for the cases where you want
# the two steps separated: pre-warming the heavy base in CI, rebuilding only the thin app
# layer, or pushing the base to a registry.
#
# Usage:
#   scripts/build-worker.sh                 # build base + worker
#   scripts/build-worker.sh --base-only     # build only the CUDA/torch/ASR base
#   scripts/build-worker.sh --app-only      # rebuild only the app layer (base must exist)
#
# Environment:
#   VOXMIND_WORKER_BASE_IMAGE   default: clipflow-worker-base:local
#   VOXMIND_WORKER_IMAGE        default: clipflow-worker:local
#   WORKER_POETRY_GROUPS        default: diarization
#   WORKER_TORCH_FLAVOR         default: gpu          (use "cpu" for a CUDA-less build)
#   WORKER_PRELOAD_ASR_MODELS   default: small,base

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKER_DIR="${REPO_ROOT}/worker"

BASE_IMAGE="${VOXMIND_WORKER_BASE_IMAGE:-clipflow-worker-base:local}"
WORKER_IMAGE="${VOXMIND_WORKER_IMAGE:-clipflow-worker:local}"
POETRY_GROUPS="${WORKER_POETRY_GROUPS:-diarization}"
TORCH_FLAVOR="${WORKER_TORCH_FLAVOR:-gpu}"
PRELOAD_ASR_MODELS="${WORKER_PRELOAD_ASR_MODELS:-small,base}"

build_base=1
build_app=1

case "${1:-}" in
  --base-only) build_app=0 ;;
  --app-only)  build_base=0 ;;
  "")          ;;
  *)
    echo "Unknown option: $1" >&2
    echo "Usage: $0 [--base-only|--app-only]" >&2
    exit 2
    ;;
esac

if [[ "${build_base}" -eq 1 ]]; then
  echo ">> Building worker base image: ${BASE_IMAGE}"
  echo ">> (CUDA runtime + torch/${TORCH_FLAVOR} + preloaded ASR models: ${PRELOAD_ASR_MODELS})"
  docker build \
    --file "${WORKER_DIR}/Dockerfile.gpu.base" \
    --build-arg "POETRY_INSTALL_GROUPS=${POETRY_GROUPS}" \
    --build-arg "TORCH_FLAVOR=${TORCH_FLAVOR}" \
    --build-arg "PRELOAD_ASR_MODELS=${PRELOAD_ASR_MODELS}" \
    --tag "${BASE_IMAGE}" \
    "${WORKER_DIR}"
fi

if [[ "${build_app}" -eq 1 ]]; then
  echo ">> Building worker image: ${WORKER_IMAGE} (FROM ${BASE_IMAGE})"
  docker build \
    --file "${WORKER_DIR}/Dockerfile.gpu" \
    --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
    --tag "${WORKER_IMAGE}" \
    "${WORKER_DIR}"
fi

echo ">> Done."
[[ "${build_base}" -eq 1 ]] && echo "   base:   ${BASE_IMAGE}"
[[ "${build_app}" -eq 1 ]] && echo "   worker: ${WORKER_IMAGE}"
exit 0
