"""When the deep agent must be rebuilt, and what must survive the rebuild.

Both cases here were found in a live run rather than by reading the code.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from tests.conftest import connect_with_ticket

from alfresco_curator.web import routes


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch):
    """Record every deep-agent build and the ledger it was handed."""
    builds = []

    def fake_build(client, telemetry, *, audit=None, allow_writes=False, use_backup_model=False):
        builds.append({"client": client, "audit": audit, "allow_writes": allow_writes})
        return Mock(name=f"agent-{len(builds)}")

    monkeypatch.setattr(routes.agent, "build_deep_agent", fake_build)
    monkeypatch.setattr(
        routes.agent,
        "run_deep_agent",
        lambda agent, prompt, thread_id="default": {
            "prompt": prompt,
            "report": "",
            "steps": [],
            "todos": [],
            "messageCount": 0,
            "pending": [],
        },
    )
    return builds


def deep(client: TestClient, prompt: str, writes: bool):
    response = client.post("/api/agent/deep", json={"prompt": prompt, "allowWrites": writes})
    assert response.status_code == 200, response.text
    return response.json()


class TestRebuild:
    def test_switching_between_reading_and_acting_rebuilds(self, client, built):
        connect_with_ticket(client)
        deep(client, "what can you do", False)
        deep(client, "tag the invoices", True)
        assert len(built) == 2
        assert [b["allow_writes"] for b in built] == [False, True]

    def test_the_ledger_survives_that_rebuild(self, client, built):
        """Routing sets allow_writes per request, so one question between two
        changes rebuilds the agent. Resetting the ledger there would throw away
        the undo history for changes already made."""
        connect_with_ticket(client)
        deep(client, "tag the invoices", True)
        deep(client, "what can you do", False)
        deep(client, "tag some more", True)
        ledgers = [b["audit"] for b in built]
        assert ledgers[0] is ledgers[1] is ledgers[2]

    def test_the_same_settings_reuse_the_agent(self, client, built):
        connect_with_ticket(client)
        deep(client, "one", True)
        deep(client, "two", True)
        assert len(built) == 1

    def test_reconnecting_rebuilds_so_no_closed_client_is_held(self, client, built):
        """The tools close over the repository client. After a reconnect the
        old one is closed, and an agent still holding it fails every call with
        "the client has been closed" - after approval has already been given."""
        connect_with_ticket(client)
        deep(client, "tag the invoices", True)

        connect_with_ticket(client)  # a fresh ticket builds a fresh client
        deep(client, "tag the invoices", True)

        assert len(built) == 2
        assert built[0]["client"] is not built[1]["client"]

    def test_a_reconnect_starts_a_new_ledger(self, client, built):
        """A new connection cannot vouch for what the previous one changed."""
        connect_with_ticket(client)
        deep(client, "tag the invoices", True)
        connect_with_ticket(client)
        deep(client, "tag the invoices", True)
        assert built[0]["audit"] is not built[1]["audit"]
