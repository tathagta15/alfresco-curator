"""An expired borrowed ticket must be distinguishable from other failures."""

from __future__ import annotations

import base64

import httpx
import pytest

from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.errors import AlfrescoError, SessionExpiredError


def _client(handler) -> AlfrescoClient:
    client = AlfrescoClient("http://acs.test:8080")
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    return client


class TestExpiredTicket:
    def test_ticket_session_raises_session_expired(self):
        """No password is held, so the request cannot be replayed."""
        client = _client(lambda request: httpx.Response(401, json={"error": {}}))
        client.ticket = "TICKET_stale"

        with pytest.raises(SessionExpiredError):
            client.get_current_user()

    def test_session_expired_is_an_alfresco_error(self):
        """Callers that do not care about the distinction still catch it."""
        assert issubclass(SessionExpiredError, AlfrescoError)

    def test_password_session_reauthenticates_instead(self):
        """A password session renews itself and must not raise."""
        calls = {"people": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tickets"):
                return httpx.Response(201, json={"entry": {"id": "TICKET_new"}})
            calls["people"] += 1
            if calls["people"] == 1:
                return httpx.Response(401, json={"error": {}})
            return httpx.Response(200, json={"entry": {"id": "admin"}})

        client = _client(handler)
        client.username, client._password = "admin", "admin"

        assert client.get_current_user()["id"] == "admin"

    def test_expired_ticket_surfaces_as_401_not_400(self, monkeypatch):
        """The embedded UI keys off 401 to fetch a fresh ticket from Share."""
        from fastapi.testclient import TestClient

        from alfresco_curator.web.app import create_app

        state = {"valid": True}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/people/-me-"):
                if not state["valid"]:
                    return httpx.Response(401, json={"error": {}})
                return httpx.Response(200, json={"entry": {"id": "share.user"}})
            if path.endswith("/search"):
                return httpx.Response(401, json={"error": {}})
            return httpx.Response(404, json={"error": {}})

        original_init = AlfrescoClient.__init__

        def patched_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self._client = httpx.Client(
                base_url=self.base_url, transport=httpx.MockTransport(handler)
            )

        monkeypatch.setattr(AlfrescoClient, "__init__", patched_init)

        with TestClient(create_app()) as client:
            connect = client.post(
                "/api/connect",
                json={"base_url": "http://acs.test:8080", "ticket": "TICKET_abc"},
            )
            assert connect.status_code == 200, connect.text

            state["valid"] = False
            response = client.post("/api/search/raw", json={"query": 'TYPE:"cm:content"'})
            assert response.status_code == 401, response.text


def test_base64_ticket_header_is_used():
    """Regression guard: the raw ticket in the header is rejected by Alfresco."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"entry": {"id": "share.user"}})

    client = _client(handler)
    client.login_with_ticket("TICKET_abc")

    encoded = base64.b64encode(b"TICKET_abc").decode()
    assert seen["auth"] == f"Basic {encoded}"
