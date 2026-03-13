from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.settings import settings


app = FastAPI(
    title=settings.api_name,
    version=settings.api_version,
    docs_url="/docs",
    redoc_url=None,
)


# =========================================================
# CORS
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://sanninjiraiya.lab",
        "http://sanninjiraiya.lab",
    ],
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
    return JSONResponse({"status": "ready"})


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