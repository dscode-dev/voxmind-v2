import csv
import io
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.audit_log import AuditLog
from app.models.private_scheduler_profile import PrivateSchedulerProfile
from app.models.private_scheduler_run import PrivateSchedulerRun
from app.models.user import User
from app.security.auth_middleware import get_current_admin
from app.services.audit_service import AuditService
from app.services.private_scheduler_service import PrivateSchedulerService


router = APIRouter(prefix="/admin", tags=["admin"])
audit_service = AuditService()
private_scheduler_service = PrivateSchedulerService()


class PrivateSchedulerCreateInput(BaseModel):
    name: str = Field(..., min_length=3, max_length=120)
    topic_label: str = Field(..., min_length=3, max_length=255)
    product_id: str
    seed_urls: list[str] = Field(default_factory=list)
    schedule_hours: list[int] = Field(default_factory=list)
    clip_mode: str = Field(default="short")
    video_ratio: str = Field(default="portrait")
    timezone_name: str = Field(default="America/Recife")
    discovery_mode: str = Field(default="seed_urls")
    is_active: bool = Field(default=True)


def _apply_audit_filters(
    query,
    *,
    action: str | None = None,
    outcome: str | None = None,
    ip_address: str | None = None,
    phone_number: str | None = None,
):
    if action:
        query = query.filter(AuditLog.action == action)
    if outcome:
        query = query.filter(AuditLog.outcome == outcome)
    if ip_address:
        query = query.filter(AuditLog.ip_address == ip_address)
    if phone_number:
        query = query.filter(AuditLog.metadata_json["phone_number"].astext == phone_number)
    return query


@router.get("/audit-logs")
def list_audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    action: str | None = None,
    outcome: str | None = None,
    ip_address: str | None = None,
    phone_number: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    query = _apply_audit_filters(
        db.query(AuditLog),
        action=action,
        outcome=outcome,
        ip_address=ip_address,
        phone_number=phone_number,
    ).order_by(AuditLog.created_at.desc())
    logs = query.limit(limit).all()

    return [
        {
            "id": str(log.id),
            "created_at": log.created_at,
            "action": log.action,
            "outcome": log.outcome,
            "actor_user_id": str(log.actor_user_id) if log.actor_user_id else None,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "ip_address": log.ip_address,
            "user_agent": log.user_agent,
            "metadata": log.metadata_json,
        }
        for log in logs
    ]


@router.get("/audit-logs/summary")
def audit_log_summary(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)

    base_query = db.query(AuditLog).filter(AuditLog.created_at >= last_24h)
    total_events = base_query.count()
    blocked_otp = base_query.filter(
        AuditLog.action.in_(["auth.start", "auth.verify"]),
        AuditLog.outcome == "rate_limited",
    ).count()
    failed_otp = base_query.filter(
        AuditLog.action == "auth.verify",
        AuditLog.outcome == "failed",
    ).count()
    admin_actions = base_query.filter(
        AuditLog.action.in_(["clip.review", "job.review.approve_all"])
    ).count()
    internal_calls = base_query.filter(
        AuditLog.action.like("internal.worker.%")
    ).count()

    top_ip_rows = (
        db.query(AuditLog.ip_address, func.count(AuditLog.id).label("count"))
        .filter(AuditLog.created_at >= last_24h, AuditLog.ip_address.isnot(None))
        .group_by(AuditLog.ip_address)
        .order_by(func.count(AuditLog.id).desc())
        .limit(5)
        .all()
    )

    top_action_rows = (
        db.query(AuditLog.action, func.count(AuditLog.id).label("count"))
        .filter(AuditLog.created_at >= last_24h)
        .group_by(AuditLog.action)
        .order_by(func.count(AuditLog.id).desc())
        .limit(5)
        .all()
    )

    suspicious_ip_rows = (
        db.query(AuditLog.ip_address, func.count(AuditLog.id).label("count"))
        .filter(
            AuditLog.created_at >= last_24h,
            AuditLog.ip_address.isnot(None),
            AuditLog.outcome.in_(["failed", "rate_limited"]),
            AuditLog.action.in_(["auth.start", "auth.verify"]),
        )
        .group_by(AuditLog.ip_address)
        .having(func.count(AuditLog.id) >= 5)
        .order_by(func.count(AuditLog.id).desc())
        .limit(5)
        .all()
    )

    suspicious_phone_rows = (
        db.query(
            AuditLog.metadata_json["phone_number"].astext.label("phone_number"),
            func.count(AuditLog.id).label("count"),
        )
        .filter(
            AuditLog.created_at >= last_24h,
            AuditLog.metadata_json["phone_number"].astext.isnot(None),
            AuditLog.outcome.in_(["failed", "rate_limited"]),
            AuditLog.action.in_(["auth.start", "auth.verify"]),
        )
        .group_by(AuditLog.metadata_json["phone_number"].astext)
        .having(func.count(AuditLog.id) >= 3)
        .order_by(func.count(AuditLog.id).desc())
        .limit(5)
        .all()
    )

    internal_spike = internal_calls >= 100
    blocked_otp_spike = blocked_otp >= 10
    failed_otp_spike = failed_otp >= 15

    alerts = []
    if blocked_otp_spike:
        alerts.append(
            {
                "level": "high",
                "code": "otp_rate_limited_spike",
                "title": "Pico de OTP bloqueado",
                "description": f"{blocked_otp} tentativas de OTP bloqueadas nas últimas 24h.",
            }
        )
    if failed_otp_spike:
        alerts.append(
            {
                "level": "high",
                "code": "otp_failed_spike",
                "title": "Muitas falhas de OTP",
                "description": f"{failed_otp} verificações OTP falharam nas últimas 24h.",
            }
        )
    if suspicious_ip_rows:
        alerts.append(
            {
                "level": "medium",
                "code": "repeated_auth_failures_by_ip",
                "title": "IPs com padrão suspeito",
                "description": "Há IPs com repetição alta de falhas ou bloqueios de autenticação.",
            }
        )
    if suspicious_phone_rows:
        alerts.append(
            {
                "level": "medium",
                "code": "repeated_auth_failures_by_phone",
                "title": "Telefones com padrão suspeito",
                "description": "Há telefones com múltiplas falhas ou bloqueios de OTP.",
            }
        )
    if internal_spike:
        alerts.append(
            {
                "level": "medium",
                "code": "internal_worker_call_spike",
                "title": "Volume alto do worker",
                "description": f"{internal_calls} chamadas internas do worker nas últimas 24h.",
            }
        )

    return {
        "window": "24h",
        "total_events": total_events,
        "blocked_otp_attempts": blocked_otp,
        "failed_otp_attempts": failed_otp,
        "admin_actions": admin_actions,
        "internal_worker_calls": internal_calls,
        "top_ip_addresses": [
            {"ip_address": row[0], "count": int(row[1])}
            for row in top_ip_rows
        ],
        "top_actions": [
            {"action": row[0], "count": int(row[1])}
            for row in top_action_rows
        ],
        "suspicious_ip_addresses": [
            {"ip_address": row[0], "count": int(row[1])}
            for row in suspicious_ip_rows
        ],
        "suspicious_phone_numbers": [
            {"phone_number": row[0], "count": int(row[1])}
            for row in suspicious_phone_rows
        ],
        "alerts": alerts,
    }


@router.get("/audit-logs/export")
def export_audit_logs(
    limit: int = Query(default=500, ge=1, le=2000),
    action: str | None = None,
    outcome: str | None = None,
    ip_address: str | None = None,
    phone_number: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    logs = (
        _apply_audit_filters(
            db.query(AuditLog),
            action=action,
            outcome=outcome,
            ip_address=ip_address,
            phone_number=phone_number,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "created_at",
            "action",
            "outcome",
            "actor_user_id",
            "target_type",
            "target_id",
            "ip_address",
            "user_agent",
            "phone_number",
            "metadata_json",
        ]
    )

    for log in logs:
        metadata = log.metadata_json or {}
        writer.writerow(
            [
                log.created_at.isoformat(),
                log.action,
                log.outcome,
                str(log.actor_user_id) if log.actor_user_id else "",
                log.target_type or "",
                log.target_id or "",
                log.ip_address or "",
                log.user_agent or "",
                str(metadata.get("phone_number", "")),
                metadata,
            ]
        )

    buffer.seek(0)
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="audit-logs-{timestamp}.csv"'
        },
    )


@router.get("/private-schedulers")
def list_private_schedulers(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    profiles = (
        db.query(PrivateSchedulerProfile)
        .order_by(PrivateSchedulerProfile.updated_at.desc())
        .all()
    )
    runs = (
        db.query(PrivateSchedulerRun)
        .order_by(PrivateSchedulerRun.created_at.desc())
        .limit(50)
        .all()
    )

    return {
        "profiles": [private_scheduler_service.serialize_profile(profile) for profile in profiles],
        "runs": [private_scheduler_service.serialize_run(run) for run in runs],
    }


@router.post("/private-schedulers")
def create_private_scheduler(
    payload: PrivateSchedulerCreateInput,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    profile = PrivateSchedulerProfile(
        owner_user_id=admin.id,
        product_id=payload.product_id,
        name=payload.name,
        topic_label=payload.topic_label,
        discovery_mode=payload.discovery_mode,
        clip_mode=payload.clip_mode,
        video_ratio=payload.video_ratio,
        timezone_name=payload.timezone_name,
        seed_urls_json=payload.seed_urls,
        schedule_hours_json=sorted({int(hour) for hour in payload.schedule_hours if 0 <= int(hour) <= 23}),
        metadata_json={"seed_cursor": 0},
        is_active=payload.is_active,
    )
    db.add(profile)
    audit_service.log(
        db,
        action="admin.private_scheduler.create",
        outcome="success",
        actor_user=admin,
        target_type="private_scheduler_profile",
        metadata={"name": payload.name, "topic_label": payload.topic_label},
    )
    db.commit()
    db.refresh(profile)
    return private_scheduler_service.serialize_profile(profile)


@router.post("/private-schedulers/{profile_id}/trigger")
def trigger_private_scheduler(
    profile_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    profile = db.query(PrivateSchedulerProfile).filter(PrivateSchedulerProfile.id == profile_id).first()
    if not profile:
        return {"status": "ignored"}

    slot = private_scheduler_service.current_slot_for_profile(profile)
    run, payload = private_scheduler_service.create_job_for_profile(
        db=db,
        profile=profile,
        owner=admin,
        scheduled_slot_at=slot,
        manual_trigger=True,
    )
    audit_service.log(
        db,
        action="admin.private_scheduler.trigger",
        outcome="success" if payload else "failed",
        actor_user=admin,
        target_type="private_scheduler_profile",
        target_id=str(profile.id),
        metadata={"run_id": str(run.id), "job_payload": payload},
    )
    db.commit()
    return {
        "status": "queued" if payload else "failed",
        "run": private_scheduler_service.serialize_run(run),
        "job_payload": payload,
    }
