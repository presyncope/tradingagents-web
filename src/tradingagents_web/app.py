"""The FastAPI application: pages, JSON API, static files, scheduler lifecycle."""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from tradingagents_web import cli_import
from tradingagents_web.jobs.manager import JobManager
from tradingagents_web.render import templates
from tradingagents_web.services import UserError
from tradingagents_web.settings import AppSettings
from tradingagents_web.store.db import Store

logger = logging.getLogger(__name__)

COOKIE = "ta_token"
OPEN_PATHS = ("/static/", "/login", "/healthz")


def create_app(settings: AppSettings | None = None, *, manager: JobManager | None = None,
               start_manager: bool = True) -> FastAPI:
    settings = settings or AppSettings()
    Store(settings.db_path).close()   # create the schema once, before any request
    if manager is None and start_manager:
        manager = JobManager(settings.db_path, settings.max_workers, poll_interval=settings.poll_interval)

    def import_cli_reports() -> None:
        store = Store(settings.db_path, init=False)
        try:
            found = cli_import.import_reports(store)
            if found["imported"]:
                logger.info("Imported %d CLI report(s)", len(found["imported"]))
        except Exception:
            logger.exception("Importing CLI reports failed")
        finally:
            store.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if manager is not None and start_manager:
            await manager.start()
            if settings.import_on_start:
                await asyncio.to_thread(import_cli_reports)
        yield
        if manager is not None and start_manager:
            await manager.stop()

    app = FastAPI(title="TradingAgents Web", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)
    app.state.settings = settings
    app.state.manager = manager
    app.state.templates = templates()
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    if not settings.token:
        # Without a login, only loopback names may reach the server: a site that
        # rebinds its own domain to 127.0.0.1 would otherwise pass the Origin
        # check below, its Origin and Host both being that domain.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # A page on another site can post a form to this server (localhost
        # included) and start runs that spend the user's API credits. Browsers
        # send Origin on such a post; refuse any that is not this server.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != "null" and urlsplit(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("cross-origin request refused", status_code=403)
        if settings.token and not request.url.path.startswith(OPEN_PATHS):
            supplied = request.cookies.get(COOKIE, "")
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
            if not hmac.compare_digest(supplied.encode(), settings.token.encode()):
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"detail": "unauthorized"}, status_code=401)
                return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        return await call_next(request)

    @app.exception_handler(UserError)
    async def user_error(request: Request, exc: UserError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc)}, status_code=exc.status)
        return app.state.templates.TemplateResponse(
            request, "error.html", {"message": str(exc)}, status_code=exc.status)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    from tradingagents_web.api.routes import router as api_router
    from tradingagents_web.pages.routes import router as pages_router

    app.include_router(api_router)
    app.include_router(pages_router)
    return app
