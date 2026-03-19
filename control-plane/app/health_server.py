from __future__ import annotations

import threading
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse


@dataclass
class ControlPlaneHealth:
    live: bool = True
    ready: bool = False
    status_message: str = "starting"
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def mark_ready(self, message: str = "ready") -> None:
        with self._lock:
            self.ready = True
            self.status_message = message

    def mark_not_ready(self, message: str = "not_ready") -> None:
        with self._lock:
            self.ready = False
            self.status_message = message

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "live": self.live,
                "ready": self.ready,
                "status": self.status_message,
            }


def create_health_app(health: ControlPlaneHealth) -> FastAPI:
    app = FastAPI(title="Voxmind Control Plane Health", docs_url=None, redoc_url=None)

    @app.get("/health", tags=["infra"])
    def healthcheck():
        return JSONResponse(health.snapshot())

    @app.get("/ready", tags=["infra"])
    def readiness():
        payload = health.snapshot()
        status_code = 200 if payload["ready"] else 503
        return JSONResponse(payload, status_code=status_code)

    return app


def start_health_server(
    health: ControlPlaneHealth,
    host: str,
    port: int,
) -> tuple[uvicorn.Server, threading.Thread]:
    app = create_health_app(health)
    config = uvicorn.Config(app=app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="control-plane-health", daemon=True)
    thread.start()
    return server, thread
