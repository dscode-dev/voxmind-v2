from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from sqlalchemy.exc import SQLAlchemyError

from app.api.router import api_router
from app.core.settings import settings
from app.db.session import SessionLocal
from app.services.bootstrap_service import BootstrapService


bootstrap_service = BootstrapService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db = SessionLocal()
    try:
        bootstrap_service.ensure_default_admin_safe(db)
    finally:
        db.close()
    yield


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
