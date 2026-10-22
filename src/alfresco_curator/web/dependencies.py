"""Shared request dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status

from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.diagnostics.telemetry import Telemetry
from alfresco_curator.web.sessions import Session, SessionStore

SESSION_COOKIE = "curator_session"

SessionId = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


def get_sessions(request: Request) -> SessionStore:
    return request.app.state.sessions


def get_telemetry(request: Request) -> Telemetry:
    return request.app.state.telemetry


def get_session(
    session_id: SessionId = None,
    sessions: SessionStore = Depends(get_sessions),
) -> Session:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not connected to Alfresco."
        )
    return session


def get_searcher(session: Session = Depends(get_session)) -> AlfrescoClient:
    client = session.clients.get("search")
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not connected to Alfresco."
        )
    return client


CurrentSession = Annotated[Session, Depends(get_session)]
Searcher = Annotated[AlfrescoClient, Depends(get_searcher)]
Sessions = Annotated[SessionStore, Depends(get_sessions)]
TelemetryDep = Annotated[Telemetry, Depends(get_telemetry)]
