"""Reporting the run as it happens, without changing the run.

`stream` and `run` must agree: the same work, the same final state, only the
reporting differs. A divergence here would mean the answer a user watched
being written is not the answer the API would have returned.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from alfresco_curator.agent.graph import SearchAgent


@pytest.fixture
def agent() -> SearchAgent:
    client = Mock()
    client.detect_query_language.return_value = "afts"
    client.username = "admin"
    return SearchAgent(client)


def events(agent, updates, tokens=()):
    """Drive `stream` over a canned sequence of graph outputs."""
    stream = [("updates", update) for update in updates]
    stream[1:1] = [("custom", {"token": piece}) for piece in tokens]
    with patch.object(agent.graph, "stream", return_value=iter(stream)):
        return list(agent.stream("unpaid invoices", thread_id="t"))


class TestStream:
    def test_each_stage_is_reported_as_it_finishes(self, agent):
        out = events(
            agent,
            [
                {"plan": {"afts_query": 'TEXT:"x"', "steps": [{"node": "plan", "detail": "d"}]}},
                {"search": {"total": 2, "steps": [{"node": "search", "detail": "2 hits"}]}},
            ],
        )
        assert [e["step"]["node"] for e in out if e["type"] == "step"] == ["plan", "search"]

    def test_the_report_arrives_in_pieces(self, agent):
        out = events(agent, [{"report": {"report": "ab"}}], tokens=["a", "b"])
        assert [e["text"] for e in out if e["type"] == "token"] == ["a", "b"]

    def test_the_final_state_is_what_run_would_return(self, agent):
        out = events(
            agent,
            [
                {"plan": {"afts_query": 'TEXT:"x"', "steps": [{"node": "plan", "detail": ""}]}},
                {
                    "search": {
                        "total": 2,
                        "results": [{"name": "a.pdf"}],
                        "steps": [{"node": "search", "detail": ""}],
                    }
                },
                {
                    "report": {
                        "report": "done",
                        "cause": "found",
                        "steps": [{"node": "report", "detail": ""}],
                    }
                },
            ],
        )
        final = out[-1]
        assert final["type"] == "result"
        state = final["state"]
        assert state["afts_query"] == 'TEXT:"x"'
        assert state["total"] == 2
        assert state["cause"] == "found"
        assert state["report"] == "done"

    def test_steps_accumulate_rather_than_replace(self, agent):
        """The graph concatenates steps; a stream that overwrote them would
        hand back a final state missing most of the working."""
        out = events(
            agent,
            [
                {"plan": {"steps": [{"node": "plan", "detail": ""}]}},
                {"search": {"steps": [{"node": "search", "detail": ""}]}},
                {"report": {"steps": [{"node": "report", "detail": ""}]}},
            ],
        )
        nodes = [s["node"] for s in out[-1]["state"]["steps"]]
        assert nodes == ["plan", "search", "report"]

    def test_a_result_is_always_emitted(self, agent):
        out = events(agent, [])
        assert out[-1]["type"] == "result"


class TestWrite:
    def test_without_a_listener_the_report_is_composed_in_one_piece(self, agent):
        """`invoke` has no stream writer, so the same node must still answer."""
        with (
            patch("alfresco_curator.agent.graph.get_stream_writer", side_effect=RuntimeError),
            patch("alfresco_curator.agent.graph.llm.complete", return_value="whole") as whole,
        ):
            assert agent._write("sys", "evidence") == "whole"
        whole.assert_called_once()

    def test_with_a_listener_every_piece_is_forwarded(self, agent):
        seen = []
        with (
            patch("alfresco_curator.agent.graph.get_stream_writer", return_value=seen.append),
            patch("alfresco_curator.agent.graph.llm.stream_text", return_value=iter(["a", "b"])),
        ):
            assert agent._write("sys", "evidence") == "ab"
        assert seen == [{"token": "a"}, {"token": "b"}]
