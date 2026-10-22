"""Health and session lifecycle."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response, status

from alfresco_curator import llm
from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.config import get_settings
from alfresco_curator.errors import AlfrescoError
from alfresco_curator.web.dependencies import SESSION_COOKIE, SessionId, Sessions
from alfresco_curator.web.schemas import (
    ConnectRequest,
    HealthResponse,
    LLMInfo,
    StatusResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["session"])


def _llm_info() -> LLMInfo:
    return LLMInfo(provider=llm.provider(), model=llm.model())


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        llm=_llm_info(),
        defaults={
            "baseUrl": settings.alfresco.base_url,
            "username": settings.alfresco.admin_user,
            "shareBaseUrl": settings.web.share_base_url.rstrip("/"),
        },
    )


def _status(sessions: Sessions, session_id: str | None) -> StatusResponse:
    session = sessions.get(session_id)
    clients = session.clients if session else {}
    search = clients.get("search")
    inspect = clients.get("inspect")
    return StatusResponse(
        searchingAs=search.username if search else None,
        inspectingAs=inspect.username if inspect else None,
        baseUrl=search.base_url if search else None,
        authSource=getattr(search, "auth_source", None) if search else None,
        llm=_llm_info(),
    )


@router.get("/status", response_model=StatusResponse)
def read_status(sessions: Sessions, session_id: SessionId = None) -> StatusResponse:
    return _status(sessions, session_id)


@router.post("/connect", response_model=StatusResponse)
def connect(
    body: ConnectRequest,
    response: Response,
    sessions: Sessions,
    session_id: SessionId = None,
) -> StatusResponse:
    settings = get_settings()
    try:
        client = AlfrescoClient(body.base_url or settings.alfresco.base_url)
        if body.ticket.strip():
            client.login_with_ticket(body.ticket)
            client.auth_source = "share"
        else:
            client.login(body.username, body.password)
            client.auth_source = "password"
    except AlfrescoError as exc:
        log.info("connect rejected for %s: %s", body.username or "<ticket>", exc.message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    session_id = sessions.attach(session_id, body.role, client)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite=settings.web.cookie_samesite,
        secure=settings.web.cookie_secure,
        max_age=settings.web.session_ttl_seconds,
    )
    log.info("%s connected as %s via %s", body.role, client.username, client.auth_source)
    return _status(sessions, session_id)


@router.post("/disconnect", response_model=StatusResponse)
def disconnect(
    response: Response,
    sessions: Sessions,
    session_id: SessionId = None,
) -> StatusResponse:
    sessions.discard(session_id)
    response.delete_cookie(SESSION_COOKIE)
    return StatusResponse(llm=_llm_info())
