from fastapi import FastAPI, Depends
from pydantic import BaseModel, HttpUrl, Field
from .auth import require_api_key
from .job_creator import create_worker_job
from .telegram_notify import notify

app = FastAPI(title="VoxMind Control Plane", version="0.1.0")

class JobRequest(BaseModel):
    video_url: HttpUrl
    mode: str = Field(default="v2", pattern="^(v1|v2)$")

class JobResponse(BaseModel):
    job_name: str
    namespace: str

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/v1/jobs", response_model=JobResponse, dependencies=[Depends(require_api_key)])
async def create_job(req: JobRequest):
    result = create_worker_job(video_url=str(req.video_url), mode=req.mode)
    await notify(f"✅ VoxMind job created: {result['job_name']}")
    return result
