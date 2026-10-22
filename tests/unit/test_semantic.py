"""Concept expansion: finding a document that shares a meaning, not a word."""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import Mock, patch

from alfresco_curator.agent import semantic


def reply(terms):
    return patch.object(semantic.llm, "complete_json", return_value={"terms": terms})


class TestExpand:
    def test_the_vocabulary_of_the_document_is_returned(self):
        with reply(["penetration test", "security assessment", "pentest"]):
            assert semantic.expand("vulnerability review")[0] == "penetration test"

    def test_quotes_cannot_break_out_of_the_phrase(self):
        """A term is interpolated into TEXT:"..." - a quote would end it."""
        with reply(['pen" OR TYPE:"cm:person', "audit"]):
            terms = semantic.expand("anything")
        assert all('"' not in term and "\\" not in term for term in terms)

    def test_duplicates_are_dropped(self):
        with reply(["Audit", "audit", "AUDIT", "review"]):
            assert semantic.expand("x") == ["Audit", "review"]

    def test_the_list_is_capped(self):
        with reply([f"term{i}" for i in range(40)]):
            assert len(semantic.expand("x")) == semantic.MAX_TERMS

    def test_a_model_failure_is_not_a_search_failure(self):
        with patch.object(semantic.llm, "complete_json", side_effect=RuntimeError("down")):
            assert semantic.expand("x") == []

    def test_junk_from_the_model_is_ignored(self):
        with patch.object(semantic.llm, "complete_json", return_value={"terms": "not a list"}):
            assert semantic.expand("x") == []


class TestQuery:
    def test_alternatives_are_joined_with_or(self):
        """These are different wordings of one idea; requiring all of them
        would be stricter than the query that already failed."""
        assert semantic.as_query(["a", "b"]) == 'TEXT:"a" OR TEXT:"b"'

    def test_no_terms_means_no_query(self):
        assert semantic.as_query([]) == ""


class TestRank:
    """A relaxed query matches by construction, so rows are not answers."""

    CANDIDATES: ClassVar[list[dict[str, str]]] = [
        {"name": "notify_user_email.ftl", "path": "/Data Dictionary/Email Templates"},
        {"name": "Remote_Working_Policy.pdf", "path": "/Company Home/HR"},
    ]

    def test_only_the_answers_are_kept(self):
        with patch.object(semantic.llm, "complete_json", return_value={"keep": [2]}):
            kept = semantic.rank("rules for working from home", self.CANDIDATES)
        assert [c["name"] for c in kept] == ["Remote_Working_Policy.pdf"]

    def test_keeping_nothing_is_a_valid_answer(self):
        with patch.object(semantic.llm, "complete_json", return_value={"keep": []}):
            assert semantic.rank("x", self.CANDIDATES) == []

    def test_an_unavailable_model_does_not_lose_results(self):
        """Filtering is an improvement, not a gate."""
        with patch.object(semantic.llm, "complete_json", side_effect=RuntimeError("down")):
            assert semantic.rank("x", self.CANDIDATES) == self.CANDIDATES

    def test_an_out_of_range_choice_is_ignored(self):
        with patch.object(semantic.llm, "complete_json", return_value={"keep": [99, 0, -1, 2]}):
            kept = semantic.rank("x", self.CANDIDATES)
        assert [c["name"] for c in kept] == ["Remote_Working_Policy.pdf"]

    def test_nothing_to_rank_asks_nothing(self):
        with patch.object(semantic.llm, "complete_json") as called:
            assert semantic.rank("x", []) == []
        called.assert_not_called()


class TestSearch:
    def client(self, total, names=()):
        stub = Mock()
        stub.search_afts.return_value = {
            "list": {
                "pagination": {"totalItems": total},
                "entries": [{"entry": {"id": f"n{i}", "name": n}} for i, n in enumerate(names)],
            }
        }
        return stub

    def test_a_conceptual_match_is_found(self):
        client = self.client(1, ["Penetration_Test_Report_2026.pdf"])
        with reply(["penetration test"]):
            found = semantic.search(client, "vulnerability review", language="afts")
        assert found["total"] == 1
        assert found["results"][0]["name"] == "Penetration_Test_Report_2026.pdf"

    def test_the_terms_are_reported_even_when_nothing_matches(self):
        """Knowing which vocabulary was ruled out is the useful part."""
        with reply(["penetration test", "audit"]):
            found = semantic.search(self.client(0), "vulnerability review", language="afts")
        assert found["terms"] == ["penetration test", "audit"]
        assert found["total"] == 0

    def test_no_expansion_means_no_repository_call(self):
        client = self.client(0)
        with reply([]):
            semantic.search(client, "x", language="afts")
        client.search_afts.assert_not_called()

    def test_a_repository_error_is_swallowed(self):
        client = Mock()
        client.search_afts.side_effect = RuntimeError("boom")
        with reply(["audit"]):
            assert semantic.search(client, "x", language="afts")["total"] == 0
