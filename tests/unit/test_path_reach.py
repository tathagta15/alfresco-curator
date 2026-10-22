"""A folder query that matches nothing should say which step is wrong."""

from __future__ import annotations

import pytest

from alfresco_curator.alfresco import afts


class TestPathValues:
    def test_extracts_the_path(self):
        query = 'TYPE:"cm:content" AND PATH:"/app:company_home/cm:HR//*"'
        assert afts.path_values(query) == ["/app:company_home/cm:HR//*"]

    def test_no_path_returns_empty(self):
        assert afts.path_values('TYPE:"cm:content"') == []


class TestPathPrefixes:
    def test_walks_back_up_deepest_first(self):
        assert afts.path_prefixes("/app:company_home/cm:HR/cm:Policies//*") == [
            "/app:company_home/cm:HR/cm:Policies//*",
            "/app:company_home/cm:HR//*",
            "/app:company_home//*",
        ]

    def test_immediate_children_wildcard_is_handled(self):
        assert afts.path_prefixes("/app:company_home/cm:HR/*") == [
            "/app:company_home/cm:HR//*",
            "/app:company_home//*",
        ]

    def test_path_without_wildcard(self):
        assert afts.path_prefixes("/app:company_home/cm:Finance") == [
            "/app:company_home/cm:Finance//*",
            "/app:company_home//*",
        ]

    @pytest.mark.parametrize("value", ["", "   ", "/", "//*"])
    def test_degenerate_values_are_safe(self, value):
        assert afts.path_prefixes(value) == []
