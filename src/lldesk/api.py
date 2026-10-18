from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool

from .activation import ActivationRequests
from .config import repo_root
from .db import Database
from .operator_session import AdmissionError, OperatorSession, read_db, status_readonly

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


class OperatorStart(BaseModel):
    request_id: UUID
    proposal: str
    consent: StrictBool


def create_app(root: Path | None = None) -> FastAPI:
    root = root or repo_root()
    operator = OperatorSession(root)

    @asynccontextmanager
    async def lifespan(app):
        yield
        operator.shutdown()

    app = FastAPI(title="Lighter Live Desk", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.operator_session = operator
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
        result = status_readonly(root)
        try:
            with read_db(root) as db:
                rows = db.execute(
                    "SELECT at,kind,body FROM events ORDER BY id DESC LIMIT 15"
                ).fetchall()
                control = db.execute(
                    "SELECT value FROM flags WHERE key='strategy_session'"
                ).fetchone()
                activation = db.execute(
                    "SELECT body FROM activation_requests ORDER BY command_id DESC LIMIT 1"
                ).fetchone()
                if control:
                    result["strategy_session"] = json.loads(control[0])
                if activation:
                    result["activation_request"] = json.loads(activation[0])
        except sqlite3.OperationalError:
            rows = []
        result["events"] = [{"at": x[0], "kind": x[1], "body": json.loads(x[2])} for x in rows]
        return result

    @app.get("/api/session/preview")
    def session_preview():
        return operator.preview()

    @app.post("/api/session/start")
    def session_start(request: Request, body: OperatorStart):
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
            raise HTTPException(403, "CSRF token required")
        try:
            return operator.begin(str(body.request_id), body.proposal, body.consent)
        except AdmissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                503, "Persiapan belum berhasil; periksa status sebelum mengulang"
            ) from exc

    @app.get("/api/activation/{request_id}")
    def activation_status(request_id: UUID):
        with read_db(root) as db:
            row = db.execute(
                "SELECT body FROM activation_requests WHERE request_id=?", (str(request_id),)
            ).fetchone()
        if not row:
            raise HTTPException(404, "activation request not found")
        return json.loads(row[0])

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
        with read_db(root) as db:
            intents = db.execute(
                "SELECT client_id,kind,side,qty,limit_price,state,tx_hash,created_at,updated_at FROM intents ORDER BY client_id"  # noqa: E501
            ).fetchall()
            fills = db.execute(
                "SELECT fill_id,client_id,qty,price,fee,exchange_ms,received_at FROM fills ORDER BY received_at"  # noqa: E501
            ).fetchall()
            events = db.execute(
                "SELECT at,kind,body FROM events ORDER BY id DESC LIMIT 500"
            ).fetchall()
        return {
            "generated_at": time.time(),
            "intents": intents,
            "fills": fills,
            "events": [{"at": x[0], "kind": x[1], "body": json.loads(x[2])} for x in events],
        }

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
