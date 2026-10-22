"""The three layers that turn inference into measured evidence."""

from __future__ import annotations

from unittest.mock import Mock

from alfresco_curator.alfresco.client import MatchCount
from alfresco_curator.diagnostics.search_debugger import (
    analyse_path_containment,
    analyse_tokenisation,
    summarise_acl,
)


def telemetry_splitting(mapping: dict[str, list[str]]) -> Mock:
    telemetry = Mock()
    telemetry.analyze.side_effect = lambda text, field=None: (
        {"text": text, "tokens": mapping.get(text, [text])}
    )
    return telemetry


class TestAnalyseTokenisation:
    def test_a_hyphenated_value_is_reported_as_several_terms(self):
        telemetry = telemetry_splitting(
            {
                "s3-oidc-connector_v2": ["s3", "oidc", "connector", "v2"],
                "S3-OIDC-Connector_v2 release": ["s3", "oidc", "connector", "v2", "release"],
            }
        )
        findings = analyse_tokenisation(
            telemetry,
            ['cm:title:"s3-oidc-connector_v2"'],
            {"properties": {"cm:title": "S3-OIDC-Connector_v2 release"}},
        )
        assert findings[0]["queryTokens"] == ["s3", "oidc", "connector", "v2"]
        assert findings[0]["splitsIntoSeveralTerms"] is True
        assert findings[0]["tokensPresentInStored"] is True, (
            "the terms are all there - the phrase is what fails"
        )

    def test_a_wildcard_clause_is_skipped(self):
        # A wildcard is not matched as a phrase, so tokenisation is irrelevant.
        telemetry = telemetry_splitting({})
        assert analyse_tokenisation(telemetry, ['cm:title:"*S3*"'], {}) == []

    def test_an_unanalysable_value_is_skipped(self):
        telemetry = Mock()
        telemetry.analyze.return_value = {"error": "HTTP 400"}
        assert analyse_tokenisation(telemetry, ['cm:title:"anything"'], {}) == []

    def test_a_single_token_value_is_not_flagged_as_split(self):
        telemetry = telemetry_splitting({"approved": ["approved"]})
        findings = analyse_tokenisation(
            telemetry, ['cm:description:"approved"'], {"properties": {}}
        )
        assert findings[0]["splitsIntoSeveralTerms"] is False


class TestAnalysePathContainment:
    def test_reports_when_the_node_sits_outside_the_queried_tree(self):
        client = Mock()
        client.count_matches.return_value = MatchCount(0, True)
        result = analyse_path_containment(
            client,
            'TYPE:"cm:content" AND PATH:"/app:company_home/cm:Engineering//*"',
            "abc-123",
            {"path": {"name": "/Company Home/Staging/Engineering_Backlog"}},
        )
        assert result["nodeIsInsideQueryPath"] is False
        assert result["nodePath"] == "/Company Home/Staging/Engineering_Backlog"

    def test_reports_when_the_node_is_inside(self):
        client = Mock()
        client.count_matches.return_value = MatchCount(1, True)
        result = analyse_path_containment(
            client, 'PATH:"/app:company_home/cm:Engineering//*"', "abc-123", {}
        )
        assert result["nodeIsInsideQueryPath"] is True

    def test_a_query_without_a_path_has_nothing_to_check(self):
        assert analyse_path_containment(Mock(), 'TYPE:"cm:content"', "abc-123", {}) is None


class TestSummariseAcl:
    def test_lifts_the_inheritance_flag_and_local_entries(self):
        node = {
            "permissions": {
                "isInheritanceEnabled": True,
                "locallySet": [{"authorityId": "GROUP_ARCHITECTS", "name": "Consumer"}],
                "inherited": [{"authorityId": "GROUP_EVERYONE", "name": "Consumer"}],
            }
        }
        acl = summarise_acl(node)
        assert acl["isInheritanceEnabled"] is True
        assert acl["locallySetAuthorities"] == ["GROUP_ARCHITECTS"]
        assert acl["inheritedAuthorities"] == ["GROUP_EVERYONE"]

    def test_a_node_without_permissions_is_safe(self):
        assert summarise_acl({})["isInheritanceEnabled"] is None
