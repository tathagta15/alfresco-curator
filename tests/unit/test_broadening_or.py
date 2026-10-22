"""An OR across distinct terms answers a wider question than was asked."""

from __future__ import annotations

from alfresco_curator.alfresco import afts
from alfresco_curator.diagnostics.query_generator import advisories


class TestBroadeningOrGroups:
    def test_same_field_different_values_is_broadening(self):
        query = 'TYPE:"cm:content" AND (TEXT:"expenses" OR TEXT:"policy")'
        assert afts.broadening_or_groups(query) == ['TEXT:"expenses" OR TEXT:"policy"']

    def test_same_value_across_fields_is_intended(self):
        query = 'TYPE:"cm:content" AND (cm:name:"*Northwind*" OR cm:title:"*Northwind*")'
        assert afts.broadening_or_groups(query) == []

    def test_at_the_top_level_without_brackets(self):
        assert afts.broadening_or_groups('TEXT:"a" OR TEXT:"b"') == ['TEXT:"a" OR TEXT:"b"']

    def test_property_prefixes_are_ignored_when_comparing_fields(self):
        query = '(@cm:title:"*x*" OR cm:title:"*x*")'
        assert afts.broadening_or_groups(query) == []

    def test_pure_conjunction_has_nothing_to_report(self):
        assert afts.broadening_or_groups('TEXT:"expenses" AND TEXT:"policy"') == []

    def test_mimetype_alternatives_are_left_alone(self):
        # A node has one mimetype, so "spreadsheets and CSV files" cannot be
        # expressed as a conjunction - this OR is the only correct reading.
        query = 'cm:content.mimetype:"text/csv" OR cm:content.mimetype:"application/vnd.ms-excel"'
        assert afts.broadening_or_groups(query) == []

    def test_type_alternatives_are_left_alone(self):
        assert afts.broadening_or_groups('TYPE:"cm:content" OR TYPE:"cm:folder"') == []


class TestAdvisories:
    def test_broadening_or_is_reported(self):
        warnings = advisories('TYPE:"cm:content" AND (TEXT:"expenses" OR TEXT:"policy")')
        assert any("wider than the request" in w for w in warnings)

    def test_conjunction_is_clean(self):
        assert advisories('TYPE:"cm:content" AND TEXT:"expenses" AND TEXT:"policy"') == []
