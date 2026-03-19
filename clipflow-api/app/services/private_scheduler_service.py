from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.billing_product import BillingProduct
from app.models.clip_job import ClipJob
from app.models.enums import JobStatus
from app.models.private_scheduler_profile import PrivateSchedulerProfile
from app.models.private_scheduler_run import PrivateSchedulerRun
from app.models.user import User


class PrivateSchedulerService:

    def due_profiles(self, db: Session, limit: int = 5) -> list[PrivateSchedulerProfile]:
        profiles = (
            db.query(PrivateSchedulerProfile)
            .filter(PrivateSchedulerProfile.is_active == True)
            .order_by(PrivateSchedulerProfile.updated_at.asc())
            .limit(limit * 4)
            .all()
        )

        due: list[PrivateSchedulerProfile] = []
        for profile in profiles:
            if self._is_due_now(db, profile):
                due.append(profile)
            if len(due) >= limit:
                break
        return due

    def create_job_for_profile(
        self,
        db: Session,
        profile: PrivateSchedulerProfile,
        owner: User,
        scheduled_slot_at: datetime,
        manual_trigger: bool = False,
    ) -> tuple[PrivateSchedulerRun, dict] | None:
        source_url = self._resolve_source_url(profile)
        if not source_url:
            run = PrivateSchedulerRun(
                profile_id=profile.id,
                status="failed",
                scheduled_slot_at=scheduled_slot_at,
                topic_label=profile.topic_label,
                error_message="No source URL available",
            )
            db.add(run)
            return run, None

        product = db.query(BillingProduct).filter(BillingProduct.id == profile.product_id).first()
        if product is None:
            run = PrivateSchedulerRun(
                profile_id=profile.id,
                status="failed",
                scheduled_slot_at=scheduled_slot_at,
                topic_label=profile.topic_label,
                source_url=source_url,
                error_message="Invalid billing product",
            )
            db.add(run)
            return run, None

        job = ClipJob(
            user_id=owner.id,
            purchase_id=None,
            product_id=product.id,
            source_url=source_url,
            status=JobStatus.QUEUED,
            pipeline_stage="prepare",
            metadata_json={
                "scheduler": {
                    "profile_id": str(profile.id),
                    "profile_name": profile.name,
                    "topic_label": profile.topic_label,
                    "manual_trigger": manual_trigger,
                    "private_scheduler": True,
                }
            },
        )
        db.add(job)
        db.flush()

        run = PrivateSchedulerRun(
            profile_id=profile.id,
            job_id=job.id,
            status="queued",
            scheduled_slot_at=scheduled_slot_at,
            source_url=source_url,
            topic_label=profile.topic_label,
            metadata_json={
                "clip_mode": profile.clip_mode,
                "video_ratio": profile.video_ratio,
                "manual_trigger": manual_trigger,
            },
        )
        db.add(run)

        metadata = dict(profile.metadata_json or {})
        metadata["seed_cursor"] = self._next_seed_cursor(profile)
        profile.metadata_json = metadata

        payload = {
            "job_id": str(job.id),
            "video_url": source_url,
            "pipeline_stage": "prepare",
            "clip_mode": profile.clip_mode,
            "video_ratio": profile.video_ratio,
        }
        return run, payload

    def serialize_profile(self, profile: PrivateSchedulerProfile) -> dict:
        return {
            "id": str(profile.id),
            "name": profile.name,
            "topic_label": profile.topic_label,
            "discovery_mode": profile.discovery_mode,
            "clip_mode": profile.clip_mode,
            "video_ratio": profile.video_ratio,
            "timezone_name": profile.timezone_name,
            "seed_urls": profile.seed_urls_json or [],
            "schedule_hours": profile.schedule_hours_json or [],
            "is_active": profile.is_active,
            "product_id": str(profile.product_id),
            "metadata": profile.metadata_json or {},
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }

    def serialize_run(self, run: PrivateSchedulerRun) -> dict:
        return {
            "id": str(run.id),
            "profile_id": str(run.profile_id),
            "job_id": str(run.job_id) if run.job_id else None,
            "status": run.status,
            "scheduled_slot_at": run.scheduled_slot_at,
            "source_url": run.source_url,
            "topic_label": run.topic_label,
            "error_message": run.error_message,
            "metadata": run.metadata_json or {},
            "created_at": run.created_at,
        }

    def current_slot_for_profile(self, profile: PrivateSchedulerProfile) -> datetime:
        zone = ZoneInfo(profile.timezone_name)
        now_local = datetime.now(zone)
        slot_local = now_local.replace(minute=0, second=0, microsecond=0)
        return slot_local.astimezone(timezone.utc)

    def _is_due_now(self, db: Session, profile: PrivateSchedulerProfile) -> bool:
        zone = ZoneInfo(profile.timezone_name)
        now_local = datetime.now(zone)
        current_hour = now_local.hour
        if current_hour not in set(profile.schedule_hours_json or []):
            return False

        slot_utc = self.current_slot_for_profile(profile)
        existing = (
            db.query(PrivateSchedulerRun)
            .filter(
                PrivateSchedulerRun.profile_id == profile.id,
                PrivateSchedulerRun.scheduled_slot_at == slot_utc,
            )
            .first()
        )
        return existing is None

    def _resolve_source_url(self, profile: PrivateSchedulerProfile) -> str | None:
        seed_urls = list(profile.seed_urls_json or [])
        if not seed_urls:
            return None

        metadata = dict(profile.metadata_json or {})
        cursor = int(metadata.get("seed_cursor", 0))
        return str(seed_urls[cursor % len(seed_urls)])

    def _next_seed_cursor(self, profile: PrivateSchedulerProfile) -> int:
        seed_urls = list(profile.seed_urls_json or [])
        if not seed_urls:
            return 0
        metadata = dict(profile.metadata_json or {})
        cursor = int(metadata.get("seed_cursor", 0))
        return (cursor + 1) % len(seed_urls)
