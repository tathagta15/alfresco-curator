"""Operator precedence is a silent correctness trap in AFTS."""

from __future__ import annotations

import pytest

from alfresco_curator.alfresco import afts
from alfresco_curator.diagnostics.query_generator import advisories


class TestMixedOperators:
    def test_pure_conjunction_is_not_flagged(self):
        assert not afts.mixes_top_level_operators('TYPE:"cm:content" AND cm:name:"a"')

    def test_pure_disjunction_is_not_flagged(self):
        assert not afts.mixes_top_level_operators('cm:name:"a" OR cm:name:"b"')

    def test_unparenthesised_mix_is_flagged(self):
        query = 'TYPE:"cm:content" AND cm:name:"a" OR cm:name:"b" AND cm:title:"c"'
        assert afts.mixes_top_level_operators(query)

    def test_parenthesised_or_is_not_flagged(self):
        """The correct form: the OR is grouped, so precedence is explicit."""
        query = 'TYPE:"cm:content" AND (cm:name:"a" OR cm:name:"b")'
        assert not afts.mixes_top_level_operators(query)

    def test_advisory_explains_the_risk(self):
        query = 'TYPE:"cm:content" AND cm:name:"a" OR cm:name:"b"'
        assert any("binds tighter" in a for a in advisories(query))

    def test_no_advisory_for_a_clean_query(self):
        query = 'TYPE:"cm:content" AND (cm:name:"alpha*" OR cm:title:"alpha*")'
        assert advisories(query) == []


class TestTopLevelTerms:
    def test_terms_are_returned_across_any_operator(self):
        query = 'TYPE:"cm:content" AND cm:name:"a" OR cm:title:"b"'
        assert afts.top_level_terms(query) == [
            'TYPE:"cm:content"',
            'cm:name:"a"',
            'cm:title:"b"',
        ]

    def test_grouped_or_stays_one_term(self):
        query = 'TYPE:"cm:content" AND (cm:name:"a" OR cm:name:"b")'
        assert afts.top_level_terms(query) == [
            'TYPE:"cm:content"',
            '(cm:name:"a" OR cm:name:"b")',
        ]

    @pytest.mark.parametrize("query", ['TYPE:"cm:content"', "", "   "])
    def test_single_or_empty_query(self, query):
        assert len(afts.top_level_terms(query)) <= 1
