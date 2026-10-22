"""Shared fixtures.

Settings are cached with `lru_cache`, so any test that manipulates the
environment must clear that cache or it silently reads another test's values.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.config import get_settings
from alfresco_curator.web.app import create_app


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mock_client() -> Callable[[Callable[[httpx.Request], httpx.Response]], AlfrescoClient]:
    """Build an AlfrescoClient whose transport is driven by a handler."""

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> AlfrescoClient:
        client = AlfrescoClient("http://acs.test:8080")
        client.username, client._password = "admin", "admin"
        client._client = httpx.Client(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        return client

    return factory


# --------------------------------------------------------------- HTTP layer
# The app fixture lives here rather than in a test module, so suites that need
# it do not import one another: a test file is not a library.

TICKET = {"entry": {"id": "TICKET_abc"}}


def alfresco_handler(request: httpx.Request) -> httpx.Response:
    """A repository that answers enough for the API surface to be exercised."""
    path = request.url.path
    if path.endswith("/tickets/-me-"):
        # Share-issued ticket validation; the header carries base64(ticket).
        if "Authorization" not in request.headers:
            return httpx.Response(401, json={"error": {"briefSummary": "no ticket"}})
        return httpx.Response(200, json=TICKET)
    if path.endswith("/people/-me-"):
        auth = request.headers.get("Authorization", "")
        if "TICKET_bad" in base64.b64decode(auth.split(" ")[-1]).decode("utf-8", "ignore"):
            return httpx.Response(401, json={"error": {"briefSummary": "ticket expired"}})
        return httpx.Response(200, json={"entry": {"id": "share.user"}})
    if path.endswith("/tickets"):
        if '"wrong"' in request.read().decode():
            return httpx.Response(403, json={"error": {"briefSummary": "bad credentials"}})
        return httpx.Response(201, json=TICKET)
    if path.endswith("/search"):
        return httpx.Response(
            200,
            json={
                "list": {
                    "pagination": {"totalItems": 1},
                    "entries": [
                        {
                            "entry": {
                                "id": "abc-123",
                                "name": "spec.pdf",
                                "nodeType": "cm:content",
                                "path": {"name": "/Company Home/Engineering"},
                                "properties": {"cm:title": "Spec"},
                                "modifiedAt": "2026-09-29T10:00:00.000+0000",
                            }
                        }
                    ],
                }
            },
        )
    return httpx.Response(404, json={"error": {"briefSummary": "not found"}})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The web app, with every repository call served by the handler above."""
    original_init = AlfrescoClient.__init__

    def patched_init(self: AlfrescoClient, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]
        self._client = httpx.Client(
            base_url=self.base_url, transport=httpx.MockTransport(alfresco_handler)
        )

    monkeypatch.setattr(AlfrescoClient, "__init__", patched_init)
    with TestClient(create_app()) as test_client:
        yield test_client


def connect(
    client: TestClient, username: str = "admin", password: str = "admin", role: str = "search"
):
    return client.post(
        "/api/connect",
        json={
            "base_url": "http://acs.test:8080",
            "username": username,
            "password": password,
            "role": role,
        },
    )


def connect_with_ticket(client: TestClient, ticket: str = "TICKET_abc", role: str = "search"):
    return client.post(
        "/api/connect",
        json={"base_url": "http://acs.test:8080", "ticket": ticket, "role": role},
    )
