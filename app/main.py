"""Loopback-only entrypoint; public release requires S10 gates."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, update
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import router
from app.config import ROOT, Settings
from app.db import Action, Scan, Store
from app.errors import AppError
from app.integrations.token_store import TokenStore
from app.security import persistent_secret
from app.services.model_settings import load_saved_model


def create_app(settings: Settings | None = None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.store.migrate()
        with app.state.store.session.begin() as db:
            db.execute(
                update(Scan)
                .where(Scan.state.in_(["queued", "running"]))
                .values(state="interrupted", progress="Server restarted")
            )
            db.execute(
                update(Action)
                .where(Action.state == "executing")
                .values(
                    state="unknown",
                    result={"message": "Server restarted during write. Reconcile before further action."},
                )
            )
            cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
            for old in db.scalars(select(Scan).where(Scan.created < cutoff)):
                old.result = {**old.result, "emails": [], "actions": []}
            for legacy in db.scalars(select(Scan).where(Scan.mode == "live")):
                if any("automated" not in mail for mail in legacy.result.get("emails", [])):
                    # Old baseline classifications must not keep recommending newsletter replies.
                    legacy.result = {
                        **legacy.result,
                        "emails": [],
                        "gmail_invalidated": True,
                        "actions": [
                            item
                            for item in legacy.result.get("actions", [])
                            if item.get("source_kind") != "email"
                        ],
                        "coverage": {
                            **legacy.result.get("coverage", {}),
                            "gmail": {"state": "fresh_scan_required"},
                        },
                    }
        yield
        pending = [task for task in app.state.tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        app.state.store.engine.dispose()

    app = FastAPI(title="CareerPilot", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = Store(settings.data_dir)
    app.state.tokens = TokenStore(settings.data_dir / "token.key")
    app.state.model_setup_error = load_saved_model(settings, app.state.tokens)
    app.state.model_setup_lock = asyncio.Lock()
    app.state.tasks = {}
    app.state.gmail_lock = asyncio.Lock()
    app.state.gmail_generation = 0
    app.state.gmail_connection_generation = 0
    app.state.gmail_revoking = False
    app.state.gmail_health = {}

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.url.path.startswith("/api"):
            # Google returns via a cross-site GET; the callback validates its
            # one-time session state and expiry before exchanging the code.
            oauth_callback = request.method == "GET" and request.url.path == "/api/gmail/callback"
            if request.headers.get("sec-fetch-site") == "cross-site" and not oauth_callback:
                return JSONResponse(
                    status_code=403,
                    content={"error": "cross_site_request", "message": "Use the local app directly."},
                )
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                import secrets

                token = request.session.get("csrf")
                if not token or not secrets.compare_digest(request.headers.get("x-csrf-token", ""), token):
                    return JSONResponse(
                        status_code=403,
                        content={"error": "csrf_invalid", "message": "Refresh before changing data."},
                    )
                origin = request.headers.get("origin")
                if origin and origin != settings.base_url:
                    return JSONResponse(
                        status_code=403,
                        content={"error": "origin_invalid", "message": "Unexpected request origin."},
                    )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(
        SessionMiddleware,
        session_cookie="careerpilot_"+hashlib.sha256(settings.base_url.encode()).hexdigest()[:12],
        secret_key=persistent_secret(settings.data_dir / "session.key"),
        same_site="lax",
        https_only=False,
        max_age=3600,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    @app.exception_handler(AppError)
    async def app_error(request, error):
        return JSONResponse(status_code=error.status, content={"error": error.code, "message": error.message})

    @app.exception_handler(TimeoutError)
    async def timeout_error(request, error):
        return JSONResponse(
            status_code=504,
            content={"error": "operation_timeout", "message": "Operation exceeded time limit."},
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": settings.mode, "public_service": False}

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


app = create_app()
