"""Lucene's own trap: a term without '+' is optional, not required."""

from __future__ import annotations

from alfresco_curator.diagnostics.query_generator import advisories


class TestLuceneAdvisories:
    def test_an_optional_term_is_flagged(self):
        query = '+TYPE:"cm:content" @cm\\:title:"*S3*"'
        warnings = advisories(query, language="lucene")
        assert any("optional rather than required" in w for w in warnings)

    def test_a_fully_required_query_is_clean(self):
        query = '+TYPE:"cm:content" +@cm\\:title:"*S3*"'
        assert advisories(query, language="lucene") == []

    def test_an_unescaped_property_colon_is_flagged(self):
        query = '+TYPE:"cm:content" +@cm:title:"*S3*"'
        warnings = advisories(query, language="lucene")
        assert any("escaped colon" in w for w in warnings)

    def test_a_single_term_query_is_not_nagged(self):
        # One optional term still matches on its own, so there is nothing to warn about.
        assert advisories('TEXT:"invoice"', language="lucene") == []

    def test_afts_rules_are_not_applied_to_lucene(self):
        # '+a +b' has no top-level AND/OR, so the AFTS advisories must stay quiet.
        query = '+TYPE:"cm:content" +TEXT:"a" +TEXT:"b"'
        assert advisories(query, language="lucene") == []

    def test_lucene_rules_are_not_applied_to_afts(self):
        query = 'TYPE:"cm:content" AND cm:title:"*S3*"'
        assert advisories(query) == []
