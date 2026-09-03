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

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        wait=wait_exponential(
            min=settings.integration_retry_min_sec,
            max=settings.integration_retry_max_sec,
        ),
        stop=stop_after_attempt(settings.integration_retry_attempts),
        reraise=True,
    )
    def update_runtime(
        self,
        job_id: str,
        *,
        pipeline_stage: str,
        step: str,
        status: str,
        details: dict[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        url = f"{self.base_url}/internal/jobs/{job_id}/runtime"
        response = requests.post(
            url,
            json={
                "pipeline_stage": pipeline_stage,
                "step": step,
                "status": status,
                "details": details or {},
                "worker_id": worker_id,
            },
            headers=self._headers(),
            timeout=settings.clipflow_api_timeout_sec,
        )
        response.raise_for_status()
        return response.json()

    def update_runtime_safe(
        self,
        job_id: str,
        *,
        pipeline_stage: str,
        step: str,
        status: str,
        details: dict[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            return self.update_runtime(
                job_id=job_id,
                pipeline_stage=pipeline_stage,
                step=step,
                status=status,
                details=details,
                worker_id=worker_id,
            )
        except Exception:
            logger.exception(
                "Failed to push runtime update to ClipFlow API",
                extra={
                    "job_id": job_id,
                    "pipeline_stage": pipeline_stage,
                    "step": step,
                    "status": status,
                },
            )
            return None


    # ------------------------------------------------------------------
    # Authoritative run lifecycle (PR-STATE-01)
    #
    # The worker reports facts. It sends the step name it is executing and never a state:
    # the API owns WORKER_STAGE_TO_STATE and the transition table, so a step cannot smuggle
    # a state past the rules by naming it. Every call here is best-effort — losing a report
    # must never fail a job that is otherwise running correctly — and every one is safe to
    # repeat, because the API classifies duplicates instead of erroring on them.
    # ------------------------------------------------------------------

    def _post_run(self, path: str, *, json_body: dict | None = None, params: dict | None = None):
        url = f"{self.base_url}/internal/pipeline-runs/{path}"
        response = requests.post(
            url,
            json=json_body,
            params=params,
            headers=self._headers(),
            timeout=settings.clipflow_api_timeout_sec,
        )
        response.raise_for_status()
        return response.json()

    def report_claim_safe(
        self,
        pipeline_job_id: str,
        *,
        worker_id: str | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any] | None:
        return self._run_call_safe(
            "report_claim",
            pipeline_job_id,
            lambda: self._post_run(
                f"{pipeline_job_id}/claimed",
                params={"worker_id": worker_id, "attempt": attempt},
            ),
        )

    def report_stage_safe(
        self,
        pipeline_job_id: str,
        *,
        stage: str,
        status: str,
        worker_id: str | None = None,
        attempt: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self._run_call_safe(
            "report_stage",
            pipeline_job_id,
            lambda: self._post_run(
                f"{pipeline_job_id}/stage",
                json_body={
                    "stage": stage,
                    "status": status,
                    "worker_id": worker_id,
                    "attempt": attempt,
                    "metadata": metadata or {},
                },
            ),
        )

    def report_failure_safe(
        self,
        pipeline_job_id: str,
        *,
        error_type: str,
        error_message: str,
        attempt: int | None = None,
        worker_id: str | None = None,
        retryable: bool = False,
    ) -> dict[str, Any] | None:
        return self._run_call_safe(
            "report_failure",
            pipeline_job_id,
            lambda: self._post_run(
                f"{pipeline_job_id}/failed",
                json_body={
                    "error_type": error_type,
                    # Truncated here as well as server-side: no reason to put a stack trace
                    # on the wire to have it thrown away at the other end.
                    "error_message": str(error_message)[:2000],
                    "attempt": attempt,
                    "worker_id": worker_id,
                    "retryable": retryable,
                },
            ),
        )

    def report_retry_safe(
        self,
        pipeline_job_id: str,
        *,
        attempt: int | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self._run_call_safe(
            "report_retry",
            pipeline_job_id,
            lambda: self._post_run(
                f"{pipeline_job_id}/retrying",
                params={"attempt": attempt, "worker_id": worker_id},
            ),
        )

    def report_completion_safe(
        self,
        pipeline_job_id: str,
        *,
        publication_eligible: bool,
        publication_eligibility: dict[str, Any] | None = None,
        worker_id: str | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any] | None:
        return self._run_call_safe(
            "report_completion",
            pipeline_job_id,
            lambda: self._post_run(
                f"{pipeline_job_id}/completed",
                json_body={
                    "publication_eligible": publication_eligible,
                    "publication_eligibility": publication_eligibility or {},
                    "worker_id": worker_id,
                    "attempt": attempt,
                },
            ),
        )

    def record_ai_execution_safe(
        self,
        pipeline_job_id: str,
        *,
        provider: str,
        model: str | None = None,
        purpose: str | None = None,
        status: str = "succeeded",
        latency_ms: int | None = None,
        prompt_chars: int | None = None,
        fallback_used: bool = False,
        error_message: str | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any] | None:
        """Record one provider call.

        Token counts and cost are deliberately not sent: this provider layer does not report
        them, and a fabricated number here would surface as a real cost figure elsewhere.
        """
        return self._run_call_safe(
            "record_ai_execution",
            pipeline_job_id,
            lambda: self._post_run(
                f"{pipeline_job_id}/ai-executions",
                json_body={
                    "provider": provider,
                    "model": model,
                    "purpose": purpose,
                    "status": status,
                    "latency_ms": latency_ms,
                    "prompt_chars": prompt_chars,
                    "fallback_used": fallback_used,
                    "error_message": (error_message or None) and str(error_message)[:500],
                    "attempt": attempt,
                },
            ),
        )

    def _run_call_safe(self, operation: str, pipeline_job_id: str, call):
        if not self.enabled or not pipeline_job_id:
            return None
        try:
            return call()
        except Exception:
            logger.warning(
                f"Failed to report to the run lifecycle ({operation})",
                extra={"pipeline_job_id": pipeline_job_id, "step": operation, "status": "failed"},
                exc_info=True,
            )
            return None

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        wait=wait_exponential(
            min=settings.integration_retry_min_sec,
            max=settings.integration_retry_max_sec,
        ),
        stop=stop_after_attempt(settings.integration_retry_attempts),
        reraise=True,
    )
    def publish_event(
        self,
        *,
        service: str,
        type: str = "info",
        pipeline_job_id: str | None = None,
        stage: str | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        url = f"{self.base_url}/internal/events"
        response = requests.post(
            url,
            json={
                "service": service,
                "type": type,
                "pipeline_job_id": pipeline_job_id,
                "stage": stage,
                "message": message,
                "payload": payload,
                "worker_id": worker_id,
            },
            headers=self._headers(),
            timeout=settings.clipflow_api_timeout_sec,
        )
        response.raise_for_status()
        return response.json()

    def publish_event_safe(
        self,
        *,
        service: str,
        type: str = "info",
        pipeline_job_id: str | None = None,
        stage: str | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            return self.publish_event(
                service=service,
                type=type,
                pipeline_job_id=pipeline_job_id,
                stage=stage,
                message=message,
                payload=payload,
                worker_id=worker_id,
            )
        except Exception:
            logger.exception(
                "Failed to publish pipeline event to ClipFlow API",
                extra={
                    "pipeline_job_id": pipeline_job_id,
                    "service": service,
                    "stage": stage,
                    "step": "publish_event",
                    "status": "failed",
                },
            )
            return None

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        wait=wait_exponential(
            min=settings.integration_retry_min_sec,
            max=settings.integration_retry_max_sec,
        ),
        stop=stop_after_attempt(settings.integration_retry_attempts),
        reraise=True,
    )
    def claim_due_private_scheduler_runs(
        self,
        worker_id: str,
        limit: int = 3,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        url = f"{self.base_url}/internal/private-scheduler/claim-due"
        response = requests.post(
            url,
            params={"worker_id": worker_id, "limit": limit},
            headers=self._headers(),
            timeout=settings.clipflow_api_timeout_sec,
        )
        response.raise_for_status()
        return response.json()

    def claim_due_private_scheduler_runs_safe(
        self,
        worker_id: str,
        limit: int = 3,
    ) -> dict[str, Any] | None:
        try:
            return self.claim_due_private_scheduler_runs(worker_id=worker_id, limit=limit)
        except Exception:
            logger.exception(
                "Failed to claim due private scheduler runs",
                extra={
                    "step": "private_scheduler_claim",
                    "status": "failed",
                },
            )
            return None
