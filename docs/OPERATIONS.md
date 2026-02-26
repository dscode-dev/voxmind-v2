# Operations (Production Notes)

## Security defaults
- Control-plane uses API key (`X-Api-Key`) for job creation.
- RBAC: control-plane SA can create/watch jobs only within namespace.
- Worker does not need K8s API by default (no service account token mounted by design in next iteration; currently pod template mounts emptyDir for /work).
- Containers run as non-root and read-only root filesystem.
- Use Secrets for tokens and API keys.

## Resource management
- Worker pods are constrained by CPU/mem requests/limits.
- Job has `activeDeadlineSeconds` to avoid runaway processing.
- Job uses `ttlSecondsAfterFinished` to clean up.

## Storage
MVP writes artifacts to `/work/out` (emptyDir). Next: MinIO/S3 persistence.

## Observability
- Structured JSON logs (ready for Loki/ELK).
- Next: OpenTelemetry tracing/metrics.
