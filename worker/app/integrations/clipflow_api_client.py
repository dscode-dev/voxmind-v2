from __future__ import annotations

from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.observability import get_logger
from app.settings import settings


logger = get_logger(__name__)


class ClipFlowApiClient:

    def __init__(self) -> None:
        self.enabled = bool(settings.clipflow_api_enabled and settings.clipflow_api_base_url)
        self.base_url = (settings.clipflow_api_base_url or "").rstrip("/")

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        wait=wait_exponential(
            min=settings.integration_retry_min_sec,
            max=settings.integration_retry_max_sec,
        ),
        stop=stop_after_attempt(settings.integration_retry_attempts),
        reraise=True,
    )
    def sync_job_artifacts(
        self,
        job_id: str,
        pipeline_stage: str | None = None,
        status: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        url = f"{self.base_url}/internal/jobs/{job_id}/sync-artifacts"
        params: dict[str, Any] = {}

        if pipeline_stage:
            params["pipeline_stage"] = pipeline_stage
        if status:
            params["status"] = status
        if error_message:
            params["error_message"] = error_message

        response = requests.post(
            url,
            params=params,
            headers=self._headers(),
            timeout=settings.clipflow_api_timeout_sec,
        )
        response.raise_for_status()
        return response.json()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if settings.clipflow_api_internal_token:
            headers["X-Internal-Token"] = settings.clipflow_api_internal_token
        return headers

    def sync_job_artifacts_safe(
        self,
        job_id: str,
        pipeline_stage: str | None = None,
        status: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            return self.sync_job_artifacts(
                job_id=job_id,
                pipeline_stage=pipeline_stage,
                status=status,
                error_message=error_message,
            )
        except Exception:
            logger.exception(
                "Failed to sync job artifacts with ClipFlow API",
                extra={
                    "job_id": job_id,
                    "pipeline_stage": pipeline_stage,
                    "step": "clipflow_api_sync",
                    "status": "failed",
                },
            )
            return None
