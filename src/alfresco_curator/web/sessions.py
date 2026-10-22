"""Server-side session store.

Alfresco credentials are exchanged for a repository ticket once; the browser
only ever holds an opaque session id. Sessions expire and are capped, because
an unbounded module-level dict leaks both memory and live HTTP connections for
the lifetime of the process.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from alfresco_curator.agent import SearchAgent
from alfresco_curator.alfresco.client import AlfrescoClient

log = logging.getLogger(__name__)

Role = Literal["search", "inspect"]
ROLES: tuple[Role, ...] = ("search", "inspect")


@dataclass(slots=True)
class Session:
    clients: dict[str, AlfrescoClient] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)
    last_seen_at: float = field(default_factory=time.monotonic)
    # Built lazily: it carries the agent's conversation memory and probe cache.
    agent: SearchAgent | None = None
    # The deep agent is separate: compiling it costs a model binding, and most
    # sessions never ask for the exploratory mode.
    deep_agent: Any | None = None
    deep_writes: bool = False
    deep_backup: bool = False
    # The client the deep agent's tools closed over, so a reconnect can be
    # detected: the tools would otherwise keep using a closed one.
    deep_client: AlfrescoClient | None = None
    audit: Any | None = None

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        self.agent = None
        self.deep_agent = None
        self.deep_backup = False
        self.deep_client = None
        self.audit = None


class SessionStore:
    def __init__(self, ttl_seconds: int = 8 * 3600, max_sessions: int = 200) -> None:
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        with self._lock:
            self._evict_expired()
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_seen_at = time.monotonic()
            return session

    def attach(self, session_id: str | None, role: Role, client: AlfrescoClient) -> str:
        with self._lock:
            self._evict_expired()
            if not session_id or session_id not in self._sessions:
                session_id = secrets.token_urlsafe(32)
                self._sessions[session_id] = Session()
                self._enforce_capacity()

            session = self._sessions[session_id]
            previous = session.clients.get(role)
            if previous is not None:
                previous.close()  # reconnecting must not leak the old connection
            session.clients[role] = client
            session.last_seen_at = time.monotonic()
            return session_id

    def discard(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()

    def clear(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    # Both helpers below assume the caller already holds the lock.
    def _evict_expired(self) -> None:
        cutoff = time.monotonic() - self._ttl
        expired = [sid for sid, s in self._sessions.items() if s.last_seen_at < cutoff]
        for session_id in expired:
            self._sessions.pop(session_id).close()
        if expired:
            log.info("expired %d idle session(s)", len(expired))

    def _enforce_capacity(self) -> None:
        while len(self._sessions) > self._max:
            oldest = min(self._sessions, key=lambda sid: self._sessions[sid].last_seen_at)
            self._sessions.pop(oldest).close()
            log.warning("session cap of %d reached; evicted the least recent", self._max)
