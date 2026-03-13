from fastapi import APIRouter

from app.api import auth
from app.api import internal_worker
from app.api import job_events
from app.api import jobs
from app.api import products
from app.api import purchases
from app.api import register
from app.api import users


api_router = APIRouter()


# ==========================================
# Auth
# ==========================================

api_router.include_router(auth.router)


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
# Products
# ==========================================

api_router.include_router(products.router)


# ==========================================
# Purchases
# ==========================================

api_router.include_router(purchases.router)


# ==========================================
# Register
# ==========================================

api_router.include_router(register.router)


# ==========================================
# Users
# ==========================================

api_router.include_router(users.router)
