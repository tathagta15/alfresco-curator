"""The query language is a property of the repository, never a user choice."""

from __future__ import annotations

from unittest.mock import Mock, patch

from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.errors import AlfrescoError


def client_accepting(*languages: str) -> AlfrescoClient:
    client = AlfrescoClient.__new__(AlfrescoClient)
    client._query_language = None

    def search(_query, **kwargs):
        if kwargs.get("language") not in languages:
            raise AlfrescoError(400, "unsupported query language")
        return {"list": {"pagination": {"totalItems": 1, "hasMoreItems": False}}}

    client.search_afts = Mock(side_effect=search)  # type: ignore[method-assign]
    return client


class TestDetectQueryLanguage:
    def test_afts_is_preferred_when_the_repository_answers_it(self):
        client = client_accepting("afts", "lucene")
        assert client.detect_query_language() == "afts"

    def test_lucene_is_used_when_afts_is_refused(self):
        client = client_accepting("lucene")
        assert client.detect_query_language() == "lucene"

    def test_the_answer_is_resolved_once(self):
        client = client_accepting("afts")
        client.detect_query_language()
        client.detect_query_language()
        assert client.search_afts.call_count == 1, "the deployment cannot change under us"

    def test_a_repository_answering_nothing_falls_back_to_afts(self):
        # Better a search error the user can read than a failure to start.
        client = client_accepting()
        assert client.detect_query_language() == "afts"

    def test_an_explicit_setting_skips_detection(self):
        client = client_accepting("afts", "lucene")
        with patch("alfresco_curator.alfresco.client.get_settings") as settings:
            settings.return_value.alfresco.query_language = "lucene"
            assert client.detect_query_language() == "lucene"
        client.search_afts.assert_not_called()
