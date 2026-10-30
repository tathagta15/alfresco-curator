"""API contract tests.

These exercise the HTTP layer without a live repository: the Alfresco
transport is mocked in conftest, so routing, validation, auth gating and
error mapping are covered deterministically.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.conftest import connect, connect_with_ticket

from alfresco_curator import governance
from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.config import get_settings
from alfresco_curator.governance import policy


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

    @pytest.mark.parametrize(
        "path",
        [
            "/api/governance/posture",
            "/api/governance/ledger",
            "/api/governance/verify",
        ],
    )
    def test_governance_endpoints_require_a_session(self, client: TestClient, path: str):
        assert client.get(path).status_code == 401

    def test_ledger_only_shows_the_signed_in_users_records(self, client: TestClient):
        connect_with_ticket(client)
        governance.get_ledger().append("ask", "share.user", {"prompt": "my request"})
        governance.get_ledger().append("ask", "another.user", {"prompt": "private request"})

        response = client.get("/api/governance/ledger")

        assert response.status_code == 200
        assert [row["actor"] for row in response.json()["records"]] == ["share.user"]

    def test_posture_exposes_the_configured_controls(self, client: TestClient):
        connect_with_ticket(client)

        response = client.get("/api/governance/posture")

        assert response.status_code == 200
        assert response.json()["ledger"]["writable"] is True
        assert response.json()["documentContentLeavesTheRepository"] is False

    def test_non_admin_cannot_change_governance_policy(
        self, client: TestClient, monkeypatch, tmp_path
    ):
        policy_path = tmp_path / "governance-policy.json"
        monkeypatch.setattr(AlfrescoClient, "get_person_groups", lambda _client: [])
        monkeypatch.setattr(
            "alfresco_curator.governance.policy._policy_path", lambda _settings=None: policy_path
        )
        connect_with_ticket(client)

        response = client.put(
            "/api/governance/settings",
            json={"allowed_model_hosts": ["ollama.internal"], "minimise_metadata": True},
        )

        assert response.status_code == 403
        assert not policy_path.exists()

    def test_alfresco_admin_can_change_and_reset_governance_policy(
        self, client: TestClient, monkeypatch, tmp_path
    ):
        policy_path = tmp_path / "governance-policy.json"
        admin_group = get_settings().governance.admin_groups[0]
        tracing_default = get_settings().llm.tracing_enabled
        budget_default = get_settings().governance.mutation_budget
        monkeypatch.setattr(AlfrescoClient, "get_person_groups", lambda _client: [admin_group])
        monkeypatch.setattr(
            "alfresco_curator.governance.policy._policy_path", lambda _settings=None: policy_path
        )
        connect_with_ticket(client)

        updated = client.put(
            "/api/governance/settings",
            json={
                "allowed_model_hosts": ["ollama.internal"],
                "minimise_metadata": True,
                "trace_to_third_party": True,
                "mutation_budget": 8,
                "allow_repository_changes": False,
                "allow_access_changes": False,
                "allow_repository_actions": False,
            },
        )

        assert updated.status_code == 200, updated.text
        assert updated.json()["canManage"] is True
        assert updated.json()["allowedModelHosts"] == ["ollama.internal"]
        assert updated.json()["minimiseMetadata"] is True
        assert updated.json()["traceToThirdParty"] is True
        assert updated.json()["mutationBudget"] == 8
        assert updated.json()["allowRepositoryChanges"] is False
        assert updated.json()["allowAccessChanges"] is False
        assert updated.json()["allowRepositoryActions"] is False
        assert policy_path.exists()
        changes = [row for row in governance.get_ledger().read() if row["kind"] == "policy_change"]
        assert [row["detail"]["outcome"] for row in changes] == ["requested", "applied"]

        reset = client.delete("/api/governance/settings")

        assert reset.status_code == 200, reset.text
        assert reset.json()["overridden"] is False
        assert reset.json()["traceToThirdParty"] is tracing_default
        assert reset.json()["mutationBudget"] == budget_default
        assert reset.json()["allowRepositoryChanges"] is True
        assert reset.json()["allowAccessChanges"] is True
        assert reset.json()["allowRepositoryActions"] is True
        assert not policy_path.exists()

    def test_new_allowlist_blocks_an_already_compiled_deep_agent(
        self, client: TestClient, monkeypatch, tmp_path
    ):
        policy_path = tmp_path / "governance-policy.json"
        monkeypatch.setattr(
            "alfresco_curator.governance.policy._policy_path", lambda _settings=None: policy_path
        )
        settings = get_settings()
        active_host = policy.host_of(policy.endpoint_for(settings.llm.provider, settings.llm))
        policy.write_policy_override([f"blocked-{active_host}"], False)
        connect(client)
        session = client.app.state.sessions.get(client.cookies.get("curator_session"))
        search_client = session.clients["search"]
        session.deep_agent = object()
        session.deep_client = search_client

        response = client.post("/api/agent/deep", json={"prompt": "find invoices"})

        assert response.status_code == 400
        assert "refusing to send repository metadata" in response.json()["detail"]


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


class TestAgentSuggestions:
    def test_suggestions_require_an_alfresco_session(self, client: TestClient):
        assert client.post("/api/agent/suggestions", json={}).status_code == 401

    def test_suggestions_embed_only_acl_filtered_name_and_path(
        self, client: TestClient, monkeypatch
    ):
        captured = {}

        class FakeIndex:
            def suggestions(self, actor, documents, **context):
                captured["actor"] = actor
                captured["documents"] = documents
                captured["context"] = context
                return ["Find related documents to spec.pdf."]

        monkeypatch.setattr(
            "alfresco_curator.web.routes.agent.get_index",
            lambda *_args: FakeIndex(),
        )
        connect_with_ticket(client)

        response = client.post(
            "/api/agent/suggestions",
            json={
                "context": {"folderName": "Engineering", "folderPath": "/Engineering"},
                "recent": {
                    "prompt": "Find the S3 spec",
                    "route": "search",
                    "outcome": "found",
                    "files": [{"name": "spec.pdf", "path": "/Engineering"}],
                    "actions": ["search_repository"],
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["suggestions"] == ["Find related documents to spec.pdf."]
        assert captured["actor"] == "share.user"
        assert captured["documents"] == [
            {"id": "abc-123", "name": "spec.pdf", "path": "/Company Home/Engineering"}
        ]
        assert captured["context"]["folder_name"] == "Engineering"
        assert captured["context"]["recent_prompt"] == "Find the S3 spec"
        assert captured["context"]["recent_outcome"] == "found"
        assert captured["context"]["recent_files"] == [
            {"name": "spec.pdf", "path": "/Engineering"}
        ]


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
