from fastapi import APIRouter

from app.api import auth
from app.api import admin
from app.api import automation
from app.api import discovery
from app.api import evaluation
from app.api import internal_worker
from app.api import job_events
from app.api import jobs
from app.api import metrics
from app.api import ops_stream
from app.api import pipeline_runs
from app.api import operations
from app.api import operations_read
from app.api import publishing
from app.api import products
from app.api import purchases
from app.api import register
from app.api import script_jobs
from app.api import users


api_router = APIRouter()


# ==========================================
# Auth
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
# Evaluation (canonical windows, reproducible performance dataset)
# ==========================================

api_router.include_router(evaluation.router)


# ==========================================
# Discovery (topics, sources, candidates)
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


# ==========================================
# Products
# ==========================================

api_router.include_router(products.router)


# ==========================================
# Purchases
# ==========================================

api_router.include_router(purchases.router)


# ==========================================
# Script Jobs
# ==========================================

api_router.include_router(script_jobs.router)


# ==========================================
# Register
# ==========================================

api_router.include_router(register.router)


# ==========================================
# Users
# ==========================================

api_router.include_router(users.router)
