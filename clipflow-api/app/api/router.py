"""Every HTTP surface the application has.

Ordered the way the flow runs, not alphabetically: the operator signs in, configures what to
watch, the autonomous loop finds and admits videos, the worker reports progress, and what comes
out is published and then measured.
"""
from fastapi import APIRouter

from app.api import auth
from app.api import admin
from app.api import automation
from app.api import dashboard
from app.api import discovery
from app.api import internal_worker
from app.api import job_events
from app.api import jobs
from app.api import metrics
from app.api import ops_stream
from app.api import pipeline_runs
from app.api import operations
from app.api import operations_read
from app.api import pipelines
from app.api import publishing


api_router = APIRouter()


# ==========================================
# Auth (one operator, phone + code)
# ==========================================

api_router.include_router(auth.router)


# ==========================================
# Admin
# ==========================================

api_router.include_router(admin.router)


# ==========================================
# Automation (autonomous discovery -> selection -> admission)
# ==========================================

api_router.include_router(automation.router)


# ==========================================
# Publishing (targets, OAuth, manual publish, resolution)
# ==========================================

api_router.include_router(publishing.router)


# ==========================================
# Dashboard (o fluxo e o desempenho, numa leitura só)
# ==========================================

api_router.include_router(dashboard.router)


# ==========================================
# Operations (product health, distinct from process health)
# ==========================================

api_router.include_router(operations.router)


# ==========================================
# Operational read models (AI status, production runs)
# ==========================================

api_router.include_router(operations_read.router)


# ==========================================
# Metrics (published-video performance and content lineage)
# ==========================================

api_router.include_router(metrics.router)


# ==========================================
# Pipelines (the object an operator configures and watches)
# ==========================================

api_router.include_router(pipelines.router)


# ==========================================
# Discovery (sources, candidates, and the runs that fill them)
# ==========================================

api_router.include_router(discovery.router)


# ==========================================
# Internal Worker
# ==========================================

api_router.include_router(internal_worker.router)


# ==========================================
# Job Events
# ==========================================

api_router.include_router(job_events.router)


# ==========================================
# Jobs
# ==========================================

api_router.include_router(jobs.router)


# ==========================================
# Ops Center (events / SSE)
# ==========================================

api_router.include_router(ops_stream.router)


# ==========================================
# Pipeline runs (authoritative state lifecycle)
# ==========================================

api_router.include_router(pipeline_runs.router)
