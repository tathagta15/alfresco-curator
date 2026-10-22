"""Which combination of clauses empties a result, not just which clause is weak."""

from __future__ import annotations

from unittest.mock import Mock

from alfresco_curator.alfresco.client import MatchCount
from alfresco_curator.diagnostics.search_debugger import analyse_conflict


def client_counting(counts: dict[str, int]) -> Mock:
    client = Mock()
    client.count_matches.side_effect = lambda q, language="afts": MatchCount(counts[q], True)
    return client


class TestAnalyseConflict:
    def test_names_the_clause_that_empties_the_result(self):
        counts = {
            'TEXT:"draft"': 10,
            'TEXT:"draft" AND TEXT:"legal"': 2,
            'TEXT:"draft" AND TEXT:"legal" AND TEXT:"agreements"': 0,
        }
        steps = analyse_conflict(
            client_counting(counts), 'TEXT:"draft" AND TEXT:"legal" AND TEXT:"agreements"'
        )
        assert [s["hits"] for s in steps] == [10, 2, 0]
        assert steps[-1]["clause"] == 'TEXT:"agreements"'

    def test_stops_at_the_first_zero(self):
        counts = {'a:"1"': 5, 'a:"1" AND b:"2"': 0}
        steps = analyse_conflict(client_counting(counts), 'a:"1" AND b:"2" AND c:"3"')
        assert len(steps) == 2, "clauses after the conflict cannot revive the result"

    def test_a_single_clause_has_no_combination_to_narrow(self):
        assert analyse_conflict(Mock(), 'TEXT:"draft"') == []

    def test_a_query_with_a_top_level_or_is_not_split(self):
        # Clause isolation is only sound for a conjunction.
        assert analyse_conflict(Mock(), 'a:"1" OR b:"2"') == []
