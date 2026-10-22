"""Application factory.

Resources with a lifetime (the telemetry HTTP client, the session store) are
created and released by the lifespan handler rather than at import time, so
tests can build isolated instances and nothing leaks between them.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from alfresco_curator import logging_setup
from alfresco_curator.config import Settings, get_settings
from alfresco_curator.diagnostics.telemetry import Telemetry
from alfresco_curator.errors import AlfrescoError, CuratorError, LLMError, SessionExpiredError
from alfresco_curator.web.routes import agent, diagnose, nodes, search, session, trace
from alfresco_curator.web.sessions import SessionStore

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
NO_STORE = {"Cache-Control": "no-store"}


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()
    logging_setup.configure(config.web.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = config
        app.state.telemetry = Telemetry(config)
        app.state.sessions = SessionStore(
            ttl_seconds=config.web.session_ttl_seconds,
            max_sessions=config.web.max_sessions,
        )
        log.info(
            "curator ready - repository %s, llm %s",
            config.alfresco.base_url,
            config.llm.provider,
        )
        try:
            yield
        finally:
            app.state.sessions.clear()
            app.state.telemetry.close()

    app = FastAPI(
        title="Alfresco Curator",
        version="1.0.0",
        summary="Natural language AFTS search, search root-cause diagnosis and "
        "end-to-end indexing pipeline telemetry.",
        lifespan=lifespan,
    )

    app.include_router(session.router)
    app.include_router(search.router)
    app.include_router(agent.router)
    app.include_router(diagnose.router)
    app.include_router(trace.router)
    app.include_router(nodes.router)

    @app.exception_handler(SessionExpiredError)
    async def _session_expired(_: Request, exc: SessionExpiredError) -> JSONResponse:
        # 401 rather than 400: the embedded UI treats this as "ask Share for a
        # fresh ticket and retry", which a 400 would not distinguish.
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(AlfrescoError)
    async def _alfresco_error(_: Request, exc: AlfrescoError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(LLMError)
    async def _llm_error(_: Request, exc: LLMError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(CuratorError)
    async def _curator_error(_: Request, exc: CuratorError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Allow only the configured origins to frame the UI. X-Frame-Options has no
    # allow-list form, so frame-ancestors is the mechanism that actually works
    # for embedding in Share while refusing everyone else.
    frame_ancestors = " ".join(config.web.frame_ancestors)

    @app.middleware("http")
    async def _security_and_cache(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = f"frame-ancestors {frame_ancestors}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/static"):
            # A stale cached asset during a live demo looks like a broken app.
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """The assistant, full screen.

        The same document as the docked panel: one conversation, one layout
        that adapts. Two different front ends for the same agent was the
        inconsistency - expanding the panel used to land somewhere else.
        """
        return FileResponse(STATIC_DIR / "bot.html", headers=NO_STORE)

    @app.get("/bot", include_in_schema=False)
    def bot() -> FileResponse:
        """The same assistant, embedded in a host's floating panel."""
        return FileResponse(STATIC_DIR / "bot.html", headers=NO_STORE)

    return app
