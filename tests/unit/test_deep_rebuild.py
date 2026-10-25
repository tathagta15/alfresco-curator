"""When the deep agent must be rebuilt, and what must survive the rebuild.

Both cases here were found in a live run rather than by reading the code.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from tests.conftest import connect_with_ticket

from alfresco_curator.web import routes
from alfresco_curator.web.routes.agent import _is_quota_error


class TestSwitchableFailures:
    """Which model failures are worth rebuilding on, and which are not."""

    @pytest.mark.parametrize(
        "message",
        [
            "Error code: 429 - rate limit",
            "RESOURCE_EXHAUSTED: quota exceeded",
            "rate_limit_exceeded",
            # A gateway that will never serve the model is as switchable as a
            # daily cap, and waiting helps neither. This one arrived as a 500.
            "Error code: 403 - {'code': 'permission_denied'} This is a paid model",
            "insufficient_quota",
            "billing: payment required",
        ],
    )
    def test_a_switchable_failure_reaches_the_backup(self, message):
        assert _is_quota_error(Exception(message)) is True

    @pytest.mark.parametrize(
        "message",
        ["connection reset by peer", "model did not return valid JSON", "node not found"],
    )
    def test_an_ordinary_failure_does_not(self, message):
        assert _is_quota_error(Exception(message)) is False

    def test_wrapping_the_error_does_not_hide_its_cause(self):
        """The vendor error is re-raised as a readable HTTPException. Reading
        only the wrapper lost the tokens the backup switch depends on, so a
        switchable failure stopped switching."""
        original = RuntimeError("Error code: 403 - permission_denied: paid model")
        try:
            raise HTTPException(status_code=503, detail="The model is unavailable") from original
        except HTTPException as wrapped:
            assert _is_quota_error(wrapped) is True

    def test_a_model_outage_is_not_reported_as_an_internal_error(self, client, monkeypatch, built):
        """A 500 invites a bug report about the repository. The model being
        busy is the user's cue to try again, so it says that instead."""

        def overloaded(agent, prompt, thread_id="default"):
            raise RuntimeError("503 UNAVAILABLE. This model is experiencing high demand.")

        monkeypatch.setattr(routes.agent, "run_deep_agent", overloaded)
        connect_with_ticket(client)
        response = client.post("/api/agent/deep", json={"prompt": "hello", "allowWrites": False})
        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"].lower()


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch):
    """Record every deep-agent build and the ledger it was handed."""
    builds = []

    def fake_build(client, telemetry, *, audit=None, allow_writes=False, use_backup_model=False):
        builds.append(
            {
                "client": client,
                "audit": audit,
                "allow_writes": allow_writes,
                "use_backup_model": use_backup_model,
            }
        )
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

    def test_the_backup_model_survives_that_rebuild(self, client, built, monkeypatch):
        """Reverting to an exhausted primary failed the next turn, and the
        already-used backup flag then blocked the retry - so the switch has to
        belong to the session, like the ledger."""
        connect_with_ticket(client)

        calls = {"n": 0}

        def fail_once(agent, prompt, thread_id="default"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Error code: 403 - permission_denied: paid model")
            return {
                "prompt": prompt,
                "report": "",
                "steps": [],
                "todos": [],
                "messageCount": 0,
                "pending": [],
            }

        monkeypatch.setattr(routes.agent, "run_deep_agent", fail_once)

        deep(client, "what can you do", False)  # fails, then retries on the backup
        assert built[-1]["use_backup_model"] is True

        deep(client, "tag the invoices", True)  # rebuild for writes
        assert built[-1]["use_backup_model"] is True, "the rebuild went back to the dead primary"
