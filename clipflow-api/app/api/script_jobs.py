from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.script_job import ScriptJob
from app.models.user import User
from app.security.auth_middleware import get_current_user
from app.security.access_control import can_bypass_credits, is_admin
from app.services.script_agent_service import ScriptAgentService


router = APIRouter(prefix="/script-jobs", tags=["script-jobs"])
script_agent = ScriptAgentService()


class CreateScriptJobInput(BaseModel):
    topic: str = Field(min_length=3, max_length=500)
    platform: str = Field(default="instagram_reels", max_length=64)
    target_audience: str | None = Field(default=None, max_length=300)
    objective: str | None = Field(default=None, max_length=300)
    tone: str | None = Field(default=None, max_length=120)
    language: str = Field(default="pt-BR", max_length=32)
    target_duration_sec: int = Field(default=45, ge=20, le=600)


def _serialize_script_job(job: ScriptJob, include_output: bool = False) -> dict:
    payload = {
        "id": str(job.id),
        "status": job.status,
        "topic": job.topic,
        "platform": job.platform,
        "target_audience": job.target_audience,
        "objective": job.objective,
        "tone": job.tone,
        "language": job.language,
        "target_duration_sec": job.target_duration_sec,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "error_message": job.error_message,
    }
    if include_output:
        payload["output"] = job.output_json or {}
        payload["prompt"] = job.prompt_text
        payload["input"] = job.input_json or {}
    return payload


def _query_script_job(db: Session, user: User, script_job_id: str) -> ScriptJob:
    query = db.query(ScriptJob).filter(ScriptJob.id == script_job_id)
    if not is_admin(user):
        query = query.filter(ScriptJob.user_id == user.id)
    job = query.first()
    if not job:
        raise HTTPException(status_code=404, detail="Script job not found")
    return job


@router.post("")
def create_script_job(
    payload: CreateScriptJobInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.credits <= 0 and not can_bypass_credits(user):
        raise HTTPException(status_code=402, detail="No credits")

    input_payload = payload.model_dump()
    prompt = script_agent.build_prompt(input_payload)
    output = script_agent.generate(input_payload)

    job = ScriptJob(
        user_id=user.id,
        status="completed",
        topic=payload.topic,
        platform=payload.platform,
        target_audience=payload.target_audience,
        objective=payload.objective,
        tone=payload.tone,
        language=payload.language,
        target_duration_sec=payload.target_duration_sec,
        prompt_text=prompt,
        input_json=input_payload,
        output_json=output,
    )
    db.add(job)

    if not can_bypass_credits(user):
        user.credits -= 1
        if user.credits == 0:
            user.token_version += 1

    db.commit()
    db.refresh(job)
    return _serialize_script_job(job, include_output=True)


@router.get("")
def list_script_jobs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(ScriptJob)
    if not is_admin(user):
        query = query.filter(ScriptJob.user_id == user.id)
    jobs = query.order_by(ScriptJob.created_at.desc()).limit(50).all()
    return [_serialize_script_job(job) for job in jobs]


@router.get("/{script_job_id}")
def get_script_job(
    script_job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = _query_script_job(db, user, script_job_id)
    return _serialize_script_job(job, include_output=True)


@router.get("/{script_job_id}/output")
def get_script_job_output(
    script_job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = _query_script_job(db, user, script_job_id)
    return job.output_json or {}
