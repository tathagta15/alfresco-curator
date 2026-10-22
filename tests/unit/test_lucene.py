"""Lucene terms split on occurrence prefixes, not on AND/OR keywords."""

from __future__ import annotations

import pytest

from alfresco_curator.alfresco import lucene


class TestTopLevelTerms:
    def test_required_terms_are_split_on_whitespace(self):
        query = '+TYPE:"cm:content" +@cm\\:title:"*S3*"'
        assert lucene.top_level_terms(query) == [
            '+TYPE:"cm:content"',
            '+@cm\\:title:"*S3*"',
        ]

    def test_a_quoted_phrase_is_not_split(self):
        query = '+@cm\\:title:"S3 Cloud Storage Connector"'
        assert lucene.top_level_terms(query) == ['+@cm\\:title:"S3 Cloud Storage Connector"']

    def test_a_bracketed_group_stays_one_term(self):
        query = '+TYPE:"cm:content" +(@cm\\:title:"*S3*" OR @cm\\:name:"*S3*")'
        assert lucene.top_level_terms(query) == [
            '+TYPE:"cm:content"',
            '+(@cm\\:title:"*S3*" OR @cm\\:name:"*S3*")',
        ]

    def test_keywords_are_not_mistaken_for_terms(self):
        # Alfresco tolerates the hybrid form; the keyword is not a clause.
        query = 'TYPE:"cm:content" AND @cm\\:title:"*S3*"'
        assert lucene.top_level_terms(query) == ['TYPE:"cm:content"', '@cm\\:title:"*S3*"']

    def test_a_negated_term_is_kept(self):
        assert lucene.top_level_terms('+TYPE:"cm:content" -@cm\\:title:"*draft*"') == [
            '+TYPE:"cm:content"',
            '-@cm\\:title:"*draft*"',
        ]


class TestIsPureConjunction:
    def test_all_required_terms_may_be_isolated(self):
        assert lucene.is_pure_conjunction('+TYPE:"cm:content" +TEXT:"OIDC"') is True

    def test_an_optional_term_makes_isolation_unsound(self):
        # A bare term is SHOULD, so removing it changes the result set.
        assert lucene.is_pure_conjunction('+TYPE:"cm:content" TEXT:"OIDC"') is False

    def test_a_negated_term_is_not_a_conjunction(self):
        assert lucene.is_pure_conjunction('+TYPE:"cm:content" -TEXT:"draft"') is False

    @pytest.mark.parametrize("query", ["", "   "])
    def test_an_empty_query_is_not_a_conjunction(self, query):
        assert lucene.is_pure_conjunction(query) is False


class TestSplitTopLevelAnd:
    def test_a_conjunction_is_split(self):
        assert lucene.split_top_level_and('+TYPE:"cm:content" +TEXT:"OIDC"') == [
            '+TYPE:"cm:content"',
            '+TEXT:"OIDC"',
        ]

    def test_an_optional_term_keeps_the_query_whole(self):
        query = '+TYPE:"cm:content" TEXT:"OIDC"'
        assert lucene.split_top_level_and(query) == [query]


class TestFieldAndValue:
    def test_an_escaped_property_is_understood(self):
        assert lucene.field_and_value('+@cm\\:title:"*S3*"') == ("cm:title", "*s3*")

    def test_the_occurrence_prefix_is_ignored(self):
        assert lucene.field_and_value('-@cm\\:name:"draft"') == ("cm:name", "draft")

    def test_a_field_without_a_local_part_keeps_its_value_intact(self):
        assert lucene.field_and_value('+TYPE:"cm:content"') == ("type", "cm:content")


class TestScopeToNode:
    def test_the_node_filter_is_appended(self):
        scoped = lucene.scope_to_node('+TYPE:"cm:content"', "abc-123")
        assert scoped == '(+TYPE:"cm:content") AND ID:"workspace://SpacesStore/abc-123"'

    def test_an_unsafe_node_id_is_rejected(self):
        with pytest.raises(ValueError):
            lucene.scope_to_node('+TYPE:"cm:content"', 'x" OR "1')
