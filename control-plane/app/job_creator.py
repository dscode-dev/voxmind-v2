import hashlib
from datetime import datetime, timezone
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from .settings import settings


def _safe_job_name(prefix: str, job_id: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{ts}-{job_id[:8]}".lower()


def _load_k8s() -> None:
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()


class JobCreator:

    def create(self, *, video_url: str, job_id: str) -> str:

        _load_k8s()
        batch = client.BatchV1Api()

        job_name = _safe_job_name("voxmind", job_id)

        env = [
            client.V1EnvVar(name="VIDEO_URL", value=video_url),
            client.V1EnvVar(name="JOB_ID", value=job_id),
            client.V1EnvVar(name="PIPELINE_MODE", value="v2"),
            client.V1EnvVar(name="LOG_LEVEL", value=settings.log_level),

            # MinIO configs
            client.V1EnvVar(name="MINIO_ENDPOINT", value=settings.minio_endpoint),
            client.V1EnvVar(name="MINIO_BUCKET", value=settings.minio_bucket),
            client.V1EnvVar(name="MINIO_ROOT_USER", value=settings.minio_root_user),
            client.V1EnvVar(name="MINIO_ROOT_PASSWORD", value=settings.minio_root_password),
        ]

        resources = client.V1ResourceRequirements(
            requests={
                "cpu": settings.worker_cpu_request,
                "memory": settings.worker_mem_request,
            },
            limits={
                "cpu": settings.worker_cpu_limit,
                "memory": settings.worker_mem_limit,
            },
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
            volume_mounts=[
                client.V1VolumeMount(name="workdir", mount_path="/tmp")
            ],
        )

        pod_spec = client.V1PodSpec(
            restart_policy="Never",
            containers=[container],
            security_context=client.V1PodSecurityContext(run_as_non_root=True),
            automount_service_account_token=True,
            volumes=[
                client.V1Volume(
                    name="workdir",
                    empty_dir=client.V1EmptyDirVolumeSource()
                )
            ],
        )

        template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(
                labels={
                    "app": "voxmind-worker",
                    "job-id": job_id
                }
            ),
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
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=settings.namespace
            ),
            spec=spec,
        )

        try:
            created = batch.create_namespaced_job(
                namespace=settings.namespace,
                body=job
            )
        except ApiException as e:
            raise RuntimeError(
                f"Failed to create Job: {e.reason} {e.body}"
            ) from e

        return created.metadata.name