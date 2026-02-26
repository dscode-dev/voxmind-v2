import hashlib
from datetime import datetime, timezone
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from .settings import settings

def _safe_job_name(prefix: str, video_url: str) -> str:
    h = hashlib.sha256(video_url.encode("utf-8")).hexdigest()[:10]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{ts}-{h}".lower()

def _load_k8s() -> None:
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()

def create_worker_job(*, video_url: str, mode: str = "v2") -> dict:
    _load_k8s()
    batch = client.BatchV1Api()

    job_name = _safe_job_name("voxmind", video_url)

    env = [
        client.V1EnvVar(name="VIDEO_URL", value=video_url),
        client.V1EnvVar(name="PIPELINE_MODE", value=mode),
        client.V1EnvVar(name="VOXMIND_NAMESPACE", value=settings.namespace),
        client.V1EnvVar(name="TELEGRAM_BOT_TOKEN", value=settings.telegram_bot_token or ""),
        client.V1EnvVar(name="TELEGRAM_CHAT_ID", value=settings.telegram_chat_id or ""),
        client.V1EnvVar(name="LOG_LEVEL", value=settings.log_level),
    ]

    resources = client.V1ResourceRequirements(
        requests={"cpu": settings.worker_cpu_request, "memory": settings.worker_mem_request},
        limits={"cpu": settings.worker_cpu_limit, "memory": settings.worker_mem_limit},
    )

    container = client.V1Container(
        name="worker",
        image=settings.worker_job_image,
        image_pull_policy="IfNotPresent",
        env=env,
        resources=resources,
        security_context=client.V1SecurityContext(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            run_as_non_root=True,
        ),
        volume_mounts=[client.V1VolumeMount(name="workdir", mount_path="/work")],
    )

    pod_spec = client.V1PodSpec(
        restart_policy="Never",
        containers=[container],
        security_context=client.V1PodSecurityContext(run_as_non_root=True),
        automount_service_account_token=True,
        volumes=[client.V1Volume(name="workdir", empty_dir=client.V1EmptyDirVolumeSource())],
    )

    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": "voxmind-worker", "job": job_name}),
        spec=pod_spec,
    )

    spec = client.V1JobSpec(
        template=template,
        backoff_limit=1,
        ttl_seconds_after_finished=settings.worker_job_ttl_seconds_after_finished,
        active_deadline_seconds=settings.worker_job_active_deadline_seconds,
    )

    job = client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(name=job_name, namespace=settings.namespace),
        spec=spec,
    )

    try:
        created = batch.create_namespaced_job(namespace=settings.namespace, body=job)
    except ApiException as e:
        raise RuntimeError(f"Failed to create Job: {e.reason} {e.body}") from e

    return {"job_name": created.metadata.name, "namespace": settings.namespace}
