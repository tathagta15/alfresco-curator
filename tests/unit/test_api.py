"""API contract tests.

These exercise the HTTP layer without a live repository: the Alfresco
transport is mocked in conftest, so routing, validation, auth gating and
error mapping are covered deterministically.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.conftest import connect, connect_with_ticket


class TestShareTicketAuth:
    """The in-Share page authenticates by handing over the user's ticket."""

    def test_ticket_connects_without_a_password(self, client: TestClient):
        response = connect_with_ticket(client)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["searchingAs"] == "share.user"
        assert body["authSource"] == "share"

    def test_ticket_session_can_search(self, client: TestClient):
        connect_with_ticket(client)
        response = client.post("/api/search/raw", json={"query": 'TYPE:"cm:content"'})
        assert response.status_code == 200

    def test_rejected_ticket_is_a_400(self, client: TestClient):
        assert connect_with_ticket(client, ticket="TICKET_bad").status_code == 400

    def test_credentials_are_required_in_some_form(self, client: TestClient):
        response = client.post("/api/connect", json={"base_url": "http://acs.test:8080"})
        assert response.status_code == 422

    def test_password_login_reports_its_own_source(self, client: TestClient):
        assert connect(client).json()["authSource"] == "password"


class TestEmbedding:
    def test_frame_ancestors_allow_share(self, client: TestClient):
        policy = client.get("/").headers["content-security-policy"]
        assert "frame-ancestors" in policy
        assert "http://localhost:8180" in policy

    def test_responses_are_not_sniffable(self, client: TestClient):
        assert client.get("/api/health").headers["x-content-type-options"] == "nosniff"


class TestHealth:
    def test_health_is_public(self, client: TestClient):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["llm"]["model"]

    def test_index_page_is_served(self, client: TestClient):
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


class TestAuthGate:
    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            ("/api/search", {"prompt": "anything"}),
            ("/api/search/raw", {"query": 'TYPE:"cm:content"'}),
            ("/api/diagnose", {"query": 'TYPE:"cm:content"', "node": "abc-123"}),
            ("/api/trace", {"node": "abc-123"}),
        ],
    )
    def test_endpoints_require_a_session(self, client: TestClient, path: str, payload: dict):
        assert client.post(path, json=payload).status_code == 401

    def test_status_is_empty_before_connecting(self, client: TestClient):
        assert client.get("/api/status").json()["searchingAs"] is None


class TestConnect:
    def test_successful_connect_reports_the_identity(self, client: TestClient):
        response = connect(client)
        assert response.status_code == 200
        assert response.json()["searchingAs"] == "admin"

    def test_session_cookie_is_httponly(self, client: TestClient):
        response = connect(client)
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie.replace("samesite", "SameSite")

    def test_bad_credentials_are_rejected(self, client: TestClient):
        assert connect(client, password="wrong").status_code == 400

    def test_unknown_role_is_rejected_by_validation(self, client: TestClient):
        response = client.post(
            "/api/connect",
            json={
                "base_url": "http://acs.test:8080",
                "username": "a",
                "password": "b",
                "role": "root",
            },
        )
        assert response.status_code == 422

    def test_disconnect_clears_the_session(self, client: TestClient):
        connect(client)
        client.post("/api/disconnect")
        search = client.post("/api/search/raw", json={"query": 'TYPE:"cm:content"'})
        assert search.status_code == 401


class TestSearch:
    def test_raw_search_returns_mapped_hits(self, client: TestClient):
        connect(client)
        response = client.post("/api/search/raw", json={"query": 'TYPE:"cm:content"'})

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["results"][0]["name"] == "spec.pdf"
        assert body["results"][0]["title"] == "Spec"

    def test_malformed_query_is_rejected_before_execution(self, client: TestClient):
        connect(client)
        response = client.post("/api/search/raw", json={"query": 'cm:name:"unterminated'})
        assert response.status_code == 400
        assert "unterminated" in response.json()["detail"]

    def test_blank_query_fails_validation(self, client: TestClient):
        connect(client)
        assert client.post("/api/search/raw", json={"query": ""}).status_code == 422

    def test_max_items_is_bounded(self, client: TestClient):
        connect(client)
        response = client.post(
            "/api/search/raw", json={"query": 'TYPE:"cm:content"', "max_items": 5000}
        )
        assert response.status_code == 422


class TestDiagnose:
    def test_node_injection_is_rejected(self, client: TestClient):
        connect(client)
        response = client.post(
            "/api/diagnose", json={"query": 'TYPE:"cm:content"', "node": 'x" OR ID:"y'}
        )
        assert response.status_code == 400

    def test_blank_node_fails_validation(self, client: TestClient):
        connect(client)
        assert (
            client.post(
                "/api/diagnose", json={"query": 'TYPE:"cm:content"', "node": ""}
            ).status_code
            == 422
        )


def test_openapi_schema_is_generated(client: TestClient):
    schema = client.get("/openapi.json").json()
    assert "/api/search" in schema["paths"]
    assert "/api/trace" in schema["paths"]
