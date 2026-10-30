"""Exception hierarchy.

A single base lets the web layer map failures to status codes without
importing every subsystem, and lets callers catch broadly when they do not
care which subsystem failed.
"""

from __future__ import annotations

import re


class CuratorError(Exception):
    """Base for every error raised by this application."""


class ConfigurationError(CuratorError):
    """Settings are missing or contradictory."""


class AlfrescoError(CuratorError):
    """The repository returned a non-2xx response, or could not be reached."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"HTTP {status_code}: {message}" if status_code else message)
        self.status_code = status_code
        self.message = message


_READ_ONLY = re.compile(r"read.only mode", re.IGNORECASE)


def is_repository_read_only(exc: AlfrescoError) -> bool:
    return exc.status_code == 403 and bool(_READ_ONLY.search(f"{exc.message} {exc}"))


def explain_alfresco_error(exc: AlfrescoError) -> str:
    """Separate a repository-wide licence lock from an ordinary ACL denial."""
    if is_repository_read_only(exc):
        return (
            "The Alfresco repository is in read-only mode; no change was made. "
            "This commonly means its licence is expired or missing. Ask an "
            "Alfresco administrator to restore write access, then reconnect. "
            f"Repository detail: {exc.message}"
        )
    return str(exc)


class SessionExpiredError(AlfrescoError):
    """A borrowed ticket is no longer valid and cannot be renewed here.

    Ticket sessions hold no password, so the caller has to obtain a new ticket
    upstream - in practice, from the hosting Share page.
    """


class LLMError(CuratorError):
    """A model provider failed, or returned something unusable."""


class QueryValidationError(CuratorError):
    """An AFTS query or node reference is structurally invalid."""
