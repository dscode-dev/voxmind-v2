from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, cast, func, String
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.user import User


class AuditService:

    def log(
        self,
        db: Session,
        action: str,
        outcome: str = "success",
        actor_user: User | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            actor_user_id=actor_user.id if actor_user else None,
            action=action,
            outcome=outcome,
            target_type=target_type,
            target_id=target_id,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata_json=metadata,
        )
        db.add(entry)
        return entry

    def recent_count(
        self,
        db: Session,
        *,
        action: str,
        since_seconds: int,
        ip_address: str | None = None,
        phone_number: str | None = None,
        outcome: str | None = None,
    ) -> int:
        cutoff = datetime.utcnow() - timedelta(seconds=since_seconds)
        query = db.query(func.count(AuditLog.id)).filter(
            AuditLog.action == action,
            AuditLog.created_at >= cutoff,
        )

        if outcome:
            query = query.filter(AuditLog.outcome == outcome)
        if ip_address:
            query = query.filter(AuditLog.ip_address == ip_address)
        if phone_number:
            query = query.filter(
                cast(AuditLog.metadata_json["phone_number"], String) == phone_number
            )

        return int(query.scalar() or 0)
