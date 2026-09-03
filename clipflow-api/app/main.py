import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from sqlalchemy.exc import SQLAlchemyError

from app.api.router import api_router
from app.core.settings import settings
from app.db.session import SessionLocal
from app.services.automation_runner import AutomationRunner
from app.services.bootstrap_service import BootstrapService


# Uvicorn attaches handlers to its own loggers and leaves the root logger bare, so without
# this every ``logger.info`` in the application - the scheduler's tick reports included - is
# discarded before it reaches stderr. Uvicorn's own loggers do not propagate, so nothing is
# logged twice.
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

bootstrap_service = BootstrapService()
# One per process. Started and stopped by the lifespan below, and guarded internally against
# being started twice by a reloading dev server.
automation_runner = AutomationRunner()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db = SessionLocal()
    try:
        bootstrap_service.ensure_default_admin_safe(db)
    finally:
        db.close()

    # The loop starts regardless of AUTONOMOUS_PIPELINE_ENABLED: the kill switch is checked
    # per tick, so it can be flipped without a restart. AUTOMATION_RUNNER_ENABLED is the
    # separate lever for not running the loop in this process at all.
    if settings.automation_runner_enabled:
        await automation_runner.start()

    try:
        yield
    finally:
        # Bounded: an in-flight tick gets a chance to finish, then it is cancelled. The
        # services are transactional, so an interrupted run leaves committed work committed.
        await automation_runner.stop()


app = FastAPI(
    title=settings.api_name,
    version=settings.api_version,
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)


# =========================================================
# CORS
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# Health checks (Kubernetes)
# =========================================================

@app.get("/health", tags=["infra"])
def health():
    return JSONResponse({"status": "ok"})


@app.get("/ready", tags=["infra"])
def readiness():
    db = SessionLocal()
    try:
        if not bootstrap_service.database_ready(db):
            return JSONResponse({"status": "not_ready", "reason": "database_unavailable"}, status_code=503)

        bootstrap_service.ensure_default_admin_safe(db)
        return JSONResponse({"status": "ready"})
    except SQLAlchemyError:
        return JSONResponse({"status": "not_ready", "reason": "database_error"}, status_code=503)
    finally:
        db.close()


# =========================================================
# API
# =========================================================

app.include_router(api_router)


# =========================================================
# Root
# =========================================================

@app.get("/", tags=["infra"])
def root():
    return {
        "service": settings.api_name,
        "version": settings.api_version,
    }
