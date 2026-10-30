"""The agent escalates to infrastructure only when the query is cleared."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from alfresco_curator.agent.cache import TTLCache
from alfresco_curator.agent.graph import SearchAgent


def search_payload(total: int, names: list[str] | None = None) -> dict:
    return {
        "list": {
            "pagination": {"totalItems": total, "hasMoreItems": False},
            "entries": [
                {
                    "entry": {
                        "id": f"id-{n}",
                        "name": n,
                        "path": {"name": "/Company Home"},
                        "properties": {"cm:title": f"Title of {n}"},
                        "content": {"mimeType": "application/pdf"},
                    }
                }
                for n in (names or [])
            ],
        }
    }


@pytest.fixture
def agent() -> SearchAgent:
    client = Mock()
    client.username = "admin"
    return SearchAgent(client)


def wire_tracer(trace: Mock, status: str, summary: str, broken_at: str | None) -> None:
    """`trace_indexer` returns a (Stage, age) tuple, so a bare Mock will not do."""
    stage = Mock()
    stage.as_dict.return_value = {"stage": "Batch indexer", "status": status}
    trace.trace_indexer.return_value = (stage, 2.0)
    trace.trace_broker.return_value = stage
    trace.summarise.return_value = {
        "status": status,
        "summary": summary,
        "brokenAt": broken_at,
    }


def wire_telemetry(telemetry: Mock) -> None:
    """State is checkpointed, and a MagicMock is not serialisable."""
    telemetry.return_value.__enter__.return_value.index_stats.return_value = {"documents": 222}


class TestTTLCache:
    def test_a_repeated_key_is_produced_once(self):
        cache = TTLCache(ttl_seconds=60)
        produce = Mock(return_value=7)
        assert cache.get_or_call("k", produce) == 7
        assert cache.get_or_call("k", produce) == 7
        assert produce.call_count == 1
        assert cache.stats() == {"hits": 1, "misses": 1, "entries": 1}

    def test_an_expired_entry_is_produced_again(self):
        cache = TTLCache(ttl_seconds=0)
        produce = Mock(side_effect=[1, 2])
        assert cache.get_or_call("k", produce) == 1
        assert cache.get_or_call("k", produce) == 2

    def test_distinct_keys_do_not_collide(self):
        cache = TTLCache(ttl_seconds=60)
        assert cache.get_or_call("a", lambda: "A") == "A"
        assert cache.get_or_call("b", lambda: "B") == "B"


class TestRouting:
    def test_a_hit_skips_straight_to_the_report(self, agent):
        assert agent._found({"total": 3}) == "report"

    def test_no_hits_goes_to_forensics(self, agent):
        assert agent._found({"total": 0}) == "forensics"

    def test_a_dead_clause_is_a_complete_explanation(self, agent):
        state = {"clause_counts": [{"clause": 'a:"1"', "hits": 0}], "path_reach": []}
        assert agent._needs_infra(state) == "report", "no need to blame the pipeline"

    def test_a_dead_path_is_also_complete(self, agent):
        state = {"clause_counts": [], "path_reach": [{"path": "/x//*", "hits": 0}]}
        assert agent._needs_infra(state) == "report"

    def test_a_sound_query_escalates_to_infrastructure(self, agent):
        state = {
            "clause_counts": [{"clause": 'a:"1"', "hits": 5}, {"clause": 'b:"2"', "hits": 2}],
            "path_reach": [],
        }
        assert agent._needs_infra(state) == "infra"


class TestRecover:
    def test_dropping_a_dead_clause_shows_what_would_have_matched(self, agent):
        agent.client.search_afts.return_value = search_payload(2, ["a.pdf", "b.pdf"])
        state = {
            "prompt": "the nope report",
            "afts_query": 'TEXT:"report" AND cm:name:"*nope*"',
            "clause_counts": [
                {"clause": 'TEXT:"report"', "hits": 222},
                {"clause": 'cm:name:"*nope*"', "hits": 0},
            ],
        }
        # Ranking is judged separately; here it must not reach the network.
        with patch("alfresco_curator.agent.graph.semantic.rank", side_effect=lambda _p, c: c):
            result = agent.recover(state)
        assert result["relaxed_query"] == 'TEXT:"report"'
        assert result["relaxed_total"] == 2

    def test_results_that_answer_nothing_are_not_offered(self, agent):
        """A relaxed query matches by construction, so rows are not answers:
        dropping the specific clause left eight email templates."""
        agent.client.search_afts.return_value = search_payload(8, ["notify_user_email.ftl"])
        state = {
            "prompt": "rules for working from home",
            "afts_query": 'TEXT:"rules" AND TEXT:"working from home"',
            "clause_counts": [
                {"clause": 'TEXT:"rules"', "hits": 8},
                {"clause": 'TEXT:"working from home"', "hits": 0},
            ],
        }
        with (
            patch("alfresco_curator.agent.graph.semantic.rank", return_value=[]),
            patch("alfresco_curator.agent.graph.semantic.search") as concept,
        ):
            concept.return_value = {"terms": [], "query": "", "total": 0, "results": []}
            result = agent.recover(state)
        assert "relaxed_query" not in result
        concept.assert_called_once()

    def test_a_relaxation_down_to_scaffolding_is_not_offered(self, agent):
        """Dropping the only clause that said what the user wanted leaves a
        type filter, and running it returns whatever happens to be in the
        repository - noise wearing the shape of an answer."""
        state = {
            "prompt": "the nope report",
            "afts_query": 'TYPE:"cm:content" AND cm:name:"*nope*"',
            "clause_counts": [
                {"clause": 'TYPE:"cm:content"', "hits": 222},
                {"clause": 'cm:name:"*nope*"', "hits": 0},
            ],
        }
        with patch("alfresco_curator.agent.graph.semantic.search") as concept:
            concept.return_value = {"terms": [], "query": "", "total": 0, "results": []}
            result = agent.recover(state)
        assert "relaxed_query" not in result
        concept.assert_called_once()

    def test_nothing_to_relax_when_every_clause_matches(self, agent):
        state = {
            "afts_query": 'a:"1" AND b:"2"',
            "clause_counts": [{"clause": 'a:"1"', "hits": 1}, {"clause": 'b:"2"', "hits": 1}],
        }
        assert agent.recover(state) == {}


class TestReport:
    def test_zero_hit_postmortem_replaces_unsupported_absence_claim(self, agent):
        with patch("alfresco_curator.agent.graph.llm") as model:
            model.complete.return_value = (
                "### What happened\nThe path probe proves no content exists in Policies."
            )
            result = agent.report(
                {
                    "total": 0,
                    "cause": "query",
                    "afts_query": 'TYPE:"cm:content" AND PATH:"/Company Home/Policies//*"',
                    "path_reach": [{"path": "/Company Home/Policies", "hits": 0, "exact": True}],
                }
            )

        assert "do not prove" in result["report"]
        assert "proves no content exists" not in result["report"]
        assert "/Company Home/Policies" in result["report"]

    def test_zero_hit_postmortem_replaces_invented_counts(self, agent):
        with patch("alfresco_curator.agent.graph.llm") as model:
            model.complete.return_value = "### Why\nThere are 4,000 matching documents."
            result = agent.report(
                {
                    "total": 0,
                    "cause": "query",
                    "afts_query": 'cm:name:"missing.pdf"',
                    "clause_counts": [{"clause": 'cm:name:"missing.pdf"', "hits": 0}],
                }
            )

        assert "4,000" not in result["report"]
        assert "0 hit(s)" in result["report"]

    def test_a_dead_clause_is_reported_as_a_query_fault_not_inconclusive(self, agent):
        # "unknown" is the starting value and is truthy, so a naive `or`
        # fallback would leave a settled diagnosis labelled inconclusive.
        with patch("alfresco_curator.agent.graph.llm") as model:
            model.complete.return_value = "### What happened\nnothing"
            result = agent.report({"total": 0, "cause": "unknown", "afts_query": 'a:"1"'})
        assert result["cause"] == "query"

    def test_an_infrastructure_verdict_survives(self, agent):
        with patch("alfresco_curator.agent.graph.llm") as model:
            model.complete.return_value = "### What happened\nnothing"
            result = agent.report({"total": 0, "cause": "infrastructure", "afts_query": 'a:"1"'})
        assert result["cause"] == "infrastructure"


class TestCachingPolicy:
    def test_the_answer_is_never_served_from_cache(self, agent):
        # Re-asking is how someone checks whether indexing has caught up.
        agent.client.search_afts.side_effect = [search_payload(0), search_payload(1, ["a.pdf"])]
        assert agent._search('cm:name:"a.pdf"')["list"]["pagination"]["totalItems"] == 0
        assert agent._search('cm:name:"a.pdf"')["list"]["pagination"]["totalItems"] == 1

    def test_evidence_probes_are_cached(self, agent):
        agent.client.count_matches.return_value = Mock(value=4, exact=True)
        agent._count('a:"1"')
        agent._count('a:"1"')
        assert agent.client.count_matches.call_count == 1


class TestEndToEnd:
    def test_a_found_document_never_touches_telemetry(self, agent):
        agent.client.search_afts.return_value = search_payload(1, ["S3_Spec.pdf"])
        agent.client.count_matches.return_value = Mock(value=1, exact=True)
        with (
            patch("alfresco_curator.agent.graph.generate_query") as plan,
            patch("alfresco_curator.agent.graph.Telemetry") as telemetry,
            patch("alfresco_curator.agent.graph.llm") as model,
        ):
            plan.return_value = {"afts_query": 'cm:name:"S3_Spec.pdf"', "explanation": "x"}
            model.complete.return_value = "### What was found\nthe spec"
            state = agent.run("find the S3 spec", thread_id="t1")

        assert state["cause"] == "found"
        assert state["total"] == 1
        telemetry.assert_not_called(), "a successful search must not cost a telemetry sweep"
        assert [s["node"] for s in state["steps"]] == ["plan", "search", "report"]

    def test_a_hit_carries_the_fields_the_result_card_needs(self, agent):
        agent.client.search_afts.return_value = search_payload(1, ["S3_Spec.pdf"])
        agent.client.count_matches.return_value = Mock(value=1, exact=True)
        with (
            patch("alfresco_curator.agent.graph.generate_query") as plan,
            patch("alfresco_curator.agent.graph.llm") as model,
        ):
            plan.return_value = {"afts_query": 'cm:name:"S3_Spec.pdf"', "explanation": "x"}
            model.complete.return_value = "### What was found\nthe spec"
            state = agent.run("find the S3 spec", thread_id="card")

        hit = state["results"][0]
        assert hit["title"] == "Title of S3_Spec.pdf", "a thumbnail row needs more than a name"
        assert hit["mimeType"] == "application/pdf"
        assert hit["path"] == "/Company Home"

    def test_each_hit_is_attributed_to_the_clauses_it_satisfied(self, agent):
        agent.client.search_afts.return_value = search_payload(1, ["S3_Spec.pdf"])
        # Only the title clause matches this node; the status clause does not.
        agent.client.count_matches.side_effect = lambda q: Mock(
            value=0 if "APPROVED" in q else 1, exact=True
        )
        with (
            patch("alfresco_curator.agent.graph.generate_query") as plan,
            patch("alfresco_curator.agent.graph.llm") as model,
        ):
            plan.return_value = {
                "afts_query": 'cm:title:"*S3*" AND cm:description:"*APPROVED*"',
                "explanation": "x",
            }
            model.complete.return_value = "### What was found\nthe spec"
            state = agent.run("approved S3 spec", thread_id="attr")

        assert state["match_evidence"][0]["matchedClauses"] == ['cm:title:"*S3*"']

    def test_an_unavailable_summary_still_answers_with_the_documents(self, agent):
        from alfresco_curator.errors import LLMError

        agent.client.search_afts.return_value = search_payload(1, ["S3_Spec.pdf"])
        agent.client.count_matches.return_value = Mock(value=1, exact=True)
        with (
            patch("alfresco_curator.agent.graph.generate_query") as plan,
            patch("alfresco_curator.agent.graph.llm") as model,
        ):
            plan.return_value = {"afts_query": 'cm:name:"S3_Spec.pdf"', "explanation": "x"}
            model.complete.side_effect = LLMError("quota")
            state = agent.run("find the S3 spec", thread_id="degraded")

        assert state["cause"] == "found"
        assert "S3_Spec.pdf" in state["report"], "the hits stand without the summary"

    def test_a_sound_query_with_no_hits_reaches_the_pipeline(self, agent):
        agent.client.search_afts.return_value = search_payload(0)
        agent.client.count_matches.return_value = Mock(value=5, exact=True)

        with (
            patch("alfresco_curator.agent.graph.generate_query") as plan,
            patch("alfresco_curator.agent.graph.analyse_empty_result") as clauses,
            patch("alfresco_curator.agent.graph.analyse_path_reach", return_value=[]),
            patch("alfresco_curator.agent.graph.analyse_conflict", return_value=[]),
            patch("alfresco_curator.agent.graph.tracer") as trace,
            patch("alfresco_curator.agent.graph.Telemetry") as telemetry,
            patch("alfresco_curator.agent.graph.llm") as model,
        ):
            plan.return_value = {"afts_query": 'a:"1" AND b:"2"', "explanation": ""}
            clauses.return_value = [
                {"clause": 'a:"1"', "hits": 4},
                {"clause": 'b:"2"', "hits": 3},
            ]
            wire_tracer(trace, "ok", "healthy", None)
            wire_telemetry(telemetry)
            model.complete.return_value = "### What happened\nnothing found"
            state = agent.run("where is my document", thread_id="t2")

        assert "infra" in [s["node"] for s in state["steps"]]
        assert state["infra"]["verdict"]["status"] == "ok"
        assert state["cause"] == "query", "a healthy pipeline points back at the query"

    def test_memory_accumulates_across_turns_on_one_thread(self, agent):
        agent.client.search_afts.return_value = search_payload(1, ["a.pdf"])
        with (
            patch("alfresco_curator.agent.graph.generate_query") as plan,
            patch("alfresco_curator.agent.graph.llm") as model,
        ):
            plan.return_value = {"afts_query": 'cm:name:"a.pdf"', "explanation": ""}
            model.complete.return_value = "### What was found\na.pdf"
            agent.run("first", thread_id="shared")
            state = agent.run("second", thread_id="shared")

        assert [h["prompt"] for h in state["history"]] == ["first", "second"]

    def test_the_step_trail_is_per_turn_not_cumulative(self, agent):
        # The checkpointer restores the previous turn, so without a reset the
        # second answer would replay the first turn's path as well.
        agent.client.search_afts.return_value = search_payload(1, ["a.pdf"])
        with (
            patch("alfresco_curator.agent.graph.generate_query") as plan,
            patch("alfresco_curator.agent.graph.llm") as model,
        ):
            plan.return_value = {"afts_query": 'cm:name:"a.pdf"', "explanation": ""}
            model.complete.return_value = "### What was found\na.pdf"
            agent.run("first", thread_id="shared")
            state = agent.run("second", thread_id="shared")

        assert [s["node"] for s in state["steps"]] == ["plan", "search", "report"]

    def test_a_previous_turns_infra_verdict_does_not_leak(self, agent):
        # A failed pipeline on one question must not be reported against the
        # next one, which found its document without ever checking.
        agent.client.search_afts.return_value = search_payload(0)
        with (
            patch("alfresco_curator.agent.graph.generate_query") as plan,
            patch("alfresco_curator.agent.graph.analyse_empty_result") as clauses,
            patch("alfresco_curator.agent.graph.analyse_path_reach", return_value=[]),
            patch("alfresco_curator.agent.graph.analyse_conflict", return_value=[]),
            patch("alfresco_curator.agent.graph.tracer") as trace,
            patch("alfresco_curator.agent.graph.Telemetry") as telemetry,
            patch("alfresco_curator.agent.graph.llm") as model,
        ):
            plan.return_value = {"afts_query": 'a:"1" AND b:"2"', "explanation": ""}
            clauses.return_value = [{"clause": 'a:"1"', "hits": 4}, {"clause": 'b:"2"', "hits": 3}]
            wire_tracer(trace, "fail", "indexer down", "Batch indexer")
            wire_telemetry(telemetry)
            model.complete.return_value = "### What happened\nnothing"
            first = agent.run("missing one", thread_id="leak")
            assert first["infra"]["verdict"]["status"] == "fail"
            assert first["cause"] == "infrastructure"

            agent.client.search_afts.return_value = search_payload(1, ["a.pdf"])
            second = agent.run("findable one", thread_id="leak")

        assert second["cause"] == "found"
        assert not second["infra"], "the pipeline was never checked for this question"

    def test_threads_are_isolated(self, agent):
        agent.client.search_afts.return_value = search_payload(1, ["a.pdf"])
        with (
            patch("alfresco_curator.agent.graph.generate_query") as plan,
            patch("alfresco_curator.agent.graph.llm") as model,
        ):
            plan.return_value = {"afts_query": 'cm:name:"a.pdf"', "explanation": ""}
            model.complete.return_value = "### What was found\na.pdf"
            agent.run("first", thread_id="one")
            state = agent.run("second", thread_id="two")

        assert [h["prompt"] for h in state["history"]] == ["second"]
