"""The single-ask endpoint: one box, the server picks the engine.

These assert the dispatch, not the classifier — `test_intent` owns the
routing rules. What matters here is that the chosen route reaches the engine
it names, and that a read request never builds an agent holding write tools.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.conftest import connect_with_ticket

from alfresco_curator.web import routes


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the engines so dispatch is observable without a model."""
    seen: dict[str, Any] = {}

    def fake_search(body, session, client):
        seen["search"] = body.prompt
        return routes.agent.AgentResponse(prompt=body.prompt, cause="found", total=1)

    def fake_deep(body, session, client, telemetry):
        seen["deep"] = body.prompt
        seen["allow_writes"] = body.allow_writes
        return routes.agent.DeepAgentResponse(prompt=body.prompt, report="done")

    def fake_undo(session, client):
        seen["undo"] = True
        return routes.agent.UndoResponse(reversed=2)

    monkeypatch.setattr(routes.agent, "run_agent", fake_search)
    monkeypatch.setattr(routes.agent, "run_deep", fake_deep)
    monkeypatch.setattr(routes.agent, "undo_changes", fake_undo)
    return seen


def ask(client: TestClient, prompt: str):
    connect_with_ticket(client)
    response = client.post("/api/agent/ask", json={"prompt": prompt})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_search_reaches_the_deterministic_ladder(client, spies):
    body = ask(client, "find the Q3 invoices")
    assert body["route"] == "search"
    assert spies["search"] == "find the Q3 invoices"
    assert "deep" not in spies
    assert body["search"]["cause"] == "found"


def test_an_instruction_reaches_the_deep_agent_with_writes(client, spies):
    body = ask(client, "delete the duplicate invoices")
    assert body["route"] == "act"
    assert spies["allow_writes"] is True
    assert body["deep"]["report"] == "done"


def test_an_open_question_reaches_the_deep_agent_without_writes(client, spies):
    body = ask(client, "what can you do")
    assert body["route"] == "explore"
    assert spies["allow_writes"] is False


def test_undo_is_handled_without_consulting_a_model(client, spies):
    body = ask(client, "undo that")
    assert body["route"] == "undo"
    assert body["undo"]["reversed"] == 2
    assert "deep" not in spies and "search" not in spies


def test_the_route_is_always_explained(client, spies):
    assert ask(client, "find invoices")["reason"]


def test_only_the_named_payload_is_populated(client, spies):
    body = ask(client, "find invoices")
    assert body["deep"] is None and body["undo"] is None


class TestPageContext:
    """What the user is pointing at on screen, and the refusal when it is not there."""

    def post(self, client, prompt, context=None):
        connect_with_ticket(client)
        payload = {"prompt": prompt}
        if context is not None:
            payload["context"] = context
        response = client.post("/api/agent/ask", json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    def test_pointing_at_nothing_stops_before_any_engine_runs(self, client, spies):
        body = self.post(client, "delete these")
        assert body["route"] == "clarify"
        assert "nothing is selected" in body["message"]
        assert spies == {}, "no engine may run for an unresolved instruction"

    def test_a_selection_reaches_the_agent(self, client, spies):
        body = self.post(
            client,
            "tag these as overdue",
            {"selected": [{"id": "n-1", "name": "INV-001.pdf"}]},
        )
        assert body["route"] == "act"
        assert "INV-001.pdf" in spies["deep"]
        assert "tag these as overdue" in spies["deep"]

    def test_the_open_folder_reaches_a_search(self, client, spies):
        self.post(
            client,
            "find invoices here",
            {"folderId": "f-1", "folderPath": "/Company Home/Finance"},
        )
        assert "/Company Home/Finance" in spies["search"]

    def test_context_is_withheld_from_an_unrelated_request(self, client, spies):
        self.post(client, "find the Q3 invoices", {"folderPath": "/Company Home/Legal"})
        assert spies["search"] == "find the Q3 invoices"
