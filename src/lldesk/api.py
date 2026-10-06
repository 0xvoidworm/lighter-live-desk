from __future__ import annotations

import secrets
import time
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .activation import ActivationRequests
from .config import repo_root
from .db import Database

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "lighter.localhost"}
ALLOWED_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8765",
    "http://localhost:8765",
    "http://lighter.localhost:8765",
    "http://lighter.localhost:5173",
}
COMMANDS = {
    "LOAD_SESSION_PERMISSION",
    "START_LIVE",
    "START_WHEN_READY",
    "STOP_ENTRIES",
    "CANCEL_ENTRIES",
    "CLOSE_POSITION",
    "PROBE_ONCE",
    "DIAGNOSE_PREFLIGHT",
}


class ActivationBinding(BaseModel):
    request_id: UUID
    session_id: UUID
    account_index: int = Field(ge=0)
    parameter_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    authorization_expires_epoch: float = Field(gt=0, allow_inf_nan=False)


def create_app(root: Path | None = None) -> FastAPI:
    root = root or repo_root()
    app = FastAPI(title="Lighter Live Desk", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_ORIGINS),
        allow_methods=["GET", "POST"],
        allow_headers=["X-CSRF-Token"],
    )
    csrf = secrets.token_urlsafe(32)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        origin = request.headers.get("origin")
        if host not in ALLOWED_HOSTS or (origin and origin not in ALLOWED_ORIGINS):
            return JSONResponse({"detail": "local host/origin only"}, status_code=403)
        return await call_next(request)

    @app.get("/api/csrf")
    def get_csrf():
        return {"token": csrf}

    @app.get("/api/status")
    def status():
        db = Database(root / "state" / "desk.db")
        try:
            result = db.read_status()
            result["connected"] = time.time() - result.get("updated_at", 0) < 3
            if not result["connected"]:
                result["state"] = "DISCONNECTED"
                result["reason"] = "engine tidak mengirim heartbeat; data terakhir bukan live"
            result["events"] = db.recent_events(15)
            return result
        finally:
            db.close()

    @app.get("/api/activation/{request_id}")
    def activation_status(request_id: UUID):
        db = Database(root / "state" / "desk.db")
        try:
            record = ActivationRequests(db).read(str(request_id))
            if record is None:
                raise HTTPException(404, "activation request not found")
            return record
        finally:
            db.close()

    @app.post("/api/command/{kind}")
    def command(kind: str, request: Request, binding: ActivationBinding | None = None):
        if kind not in COMMANDS:
            raise HTTPException(404, "unknown command")
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
            raise HTTPException(403, "CSRF token required")
        db = Database(root / "state" / "desk.db")
        try:
            if time.time() - db.read_status().get("updated_at", 0) >= 3:
                raise HTTPException(503, "engine disconnected")
            if kind == "START_WHEN_READY":
                if binding is None:
                    raise HTTPException(
                        422, "explicit request/session/account/parameter/expiry binding required"
                    )
                try:
                    activation = ActivationRequests(db).submit(binding.model_dump(mode="json"))
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
                return {
                    "command_id": activation["command_id"],
                    "state": activation["status"],
                    "activation_request": activation,
                    "message": "permintaan aktivasi tercatat; belum menjadi sesi aktif",
                }
            command_id = db.command(kind)
            return {
                "command_id": command_id,
                "state": "QUEUED",
                "message": "permintaan tercatat; tunggu hasil bursa pada status",
            }
        finally:
            db.close()

    @app.get("/api/report")
    def report():
        db = Database(root / "state" / "desk.db")
        try:
            intents = db.conn.execute(
                "SELECT client_id,kind,side,qty,limit_price,state,tx_hash,created_at,updated_at "
                "FROM intents ORDER BY client_id"
            ).fetchall()
            fills = db.conn.execute(
                "SELECT fill_id,client_id,qty,price,fee,exchange_ms,received_at "
                "FROM fills ORDER BY received_at"
            ).fetchall()
            return {
                "generated_at": time.time(),
                "intents": intents,
                "fills": fills,
                "events": db.recent_events(500),
            }
        finally:
            db.close()

    dist = root / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        def frontend(path: str):
            target = dist / path
            if path and target.is_file() and target.resolve().is_relative_to(dist.resolve()):
                return FileResponse(target)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
