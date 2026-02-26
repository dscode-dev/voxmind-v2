# VoxMind V2 (Kubernetes-native)

Production-oriented, Kubernetes-native architecture for VoxMind V2.

## Components
- **control-plane**: FastAPI service that receives requests (Telegram bot or HTTP), creates Kubernetes Jobs, tracks status, and can notify Telegram.
- **worker**: Stateless container executed as a Kubernetes Job (1 video = 1 pod). Performs download → audio extraction → ASR → (next phases: chunking/LLM/cutting/rendering).

## Quick Start (cluster already running)

1) Create namespace + RBAC
```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/rbac-control-plane.yaml
```

2) Create Secrets (edit values first)
```bash
kubectl apply -f k8s/secrets.yaml
```

3) Deploy control-plane
```bash
kubectl apply -f k8s/control-plane-deployment.yaml
```

4) Build/push images and update manifests
- Build `control-plane` and `worker` images
- Push to your registry
- Update `image:` fields in `k8s/control-plane-deployment.yaml`

5) Trigger a job (example)
```bash
kubectl -n voxmind-v2 port-forward svc/voxmind-control-plane 8080:80
curl -X POST http://localhost:8080/v1/jobs \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: <CONTROL_PLANE_API_KEY>' \
  -d '{"video_url":"https://www.youtube.com/watch?v=VIDEO_ID","mode":"v2"}'
```

## MVP Scope
This MVP focuses on **production-grade scaffolding**: RBAC, structured logs, settings, timeouts/retries, resource limits, secure defaults, and a real **download+ASR** step in the worker (CPU, faster-whisper).

Next iterations will plug in: chunking, LLM routing, scoring, rendering, subtitles, artifact storage (MinIO/S3), and Telegram ingestion.
