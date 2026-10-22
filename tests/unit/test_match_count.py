"""`totalItems` is a lower bound, not a total, whenever rows remain."""

from __future__ import annotations

from unittest.mock import Mock

from alfresco_curator.alfresco.client import AlfrescoClient, MatchCount


def client_returning(total: int, has_more: bool) -> AlfrescoClient:
    client = AlfrescoClient.__new__(AlfrescoClient)
    client.search_afts = Mock(  # type: ignore[method-assign]
        return_value={"list": {"pagination": {"totalItems": total, "hasMoreItems": has_more}}}
    )
    return client


class TestMatchCount:
    def test_a_drained_page_is_an_exact_count(self):
        counted = client_returning(222, has_more=False).count_matches("TYPE:x")
        assert counted == MatchCount(222, True)

    def test_a_full_page_reports_a_lower_bound(self):
        # Alfresco derives totalItems from the page it just returned, so it is
        # skipCount + count + 1 while more rows remain; the extra row comes off.
        count = client_returning(1001, has_more=True).count_matches("TYPE:x")
        assert count == MatchCount(1000, False)
        assert str(count) == "1000+"

    def test_counting_requests_a_full_page_of_ids(self):
        client = client_returning(5, has_more=False)
        client.count_matches("TYPE:x")
        _, kwargs = client.search_afts.call_args
        assert kwargs["max_items"] == 1000, "a one-row page cannot produce a usable count"
        assert kwargs["fields"] == ["id"], "rows are only needed for their count"
