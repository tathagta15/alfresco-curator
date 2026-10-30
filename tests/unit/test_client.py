import json

import httpx
import pytest

from alfresco_curator.alfresco import afts
from alfresco_curator.alfresco.client import AlfrescoClient, normalise_base_url
from alfresco_curator.errors import AlfrescoError


class TestBaseUrlNormalisation:
    @pytest.mark.parametrize(
        "raw",
        [
            "http://localhost:8080",
            "http://localhost:8080/",
            "http://localhost:8080/alfresco",
            "http://localhost:8080/alfresco/",
            "http://localhost:8080/share",
            "localhost:8080",
            "http://localhost:8080/alfresco/api/-default-/public",
        ],
    )
    def test_all_forms_reach_the_same_root(self, raw):
        assert normalise_base_url(raw) == "http://localhost:8080/alfresco"

    def test_https_is_preserved(self):
        assert normalise_base_url("https://acs.example.com") == "https://acs.example.com/alfresco"

    def test_empty_url_is_rejected(self):
        with pytest.raises(AlfrescoError):
            normalise_base_url("   ")


class TestSyntaxProblems:
    def test_valid_query_has_no_problems(self):
        assert afts.syntax_problems('TYPE:"cm:content" AND cm:name:"*spec*"') == []

    def test_empty_query(self):
        assert afts.syntax_problems("   ") == ["query is empty"]

    def test_unterminated_quote(self):
        assert any("unterminated" in p for p in afts.syntax_problems('cm:name:"spec'))

    def test_unclosed_bracket(self):
        problems = afts.syntax_problems('(TYPE:"cm:content" AND cm:name:"a"')
        assert any("unclosed" in p for p in problems)

    def test_unbalanced_closing_bracket(self):
        problems = afts.syntax_problems('TYPE:"cm:content")')
        assert any("unbalanced closing" in p for p in problems)

    def test_dangling_trailing_operator(self):
        assert any("dangling AND" in p for p in afts.syntax_problems('TYPE:"cm:content" AND'))

    def test_date_range_brackets_are_not_flagged(self):
        query = "cm:modified:[2026-08-01T00:00:00.000Z TO NOW]"
        assert afts.syntax_problems(query) == []

    def test_quoted_bracket_is_not_flagged(self):
        assert afts.syntax_problems('cm:name:"report (final)"') == []


class TestResolveNodeId:
    def _client(self, handler):
        client = AlfrescoClient("http://acs.test:8080")
        client.username, client._password = "admin", "admin"
        client._client = httpx.Client(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        return client

    def test_bare_uuid_passes_through(self):
        client = self._client(lambda request: httpx.Response(500))
        assert client.resolve_node_id("  abc-123  ") == "abc-123"

    def test_workspace_noderef_is_stripped(self):
        client = self._client(lambda request: httpx.Response(500))
        assert client.resolve_node_id("workspace://SpacesStore/abc-123") == "abc-123"

    def test_archive_noderef_is_stripped(self):
        client = self._client(lambda request: httpx.Response(500))
        assert client.resolve_node_id("archive://SpacesStore/abc-123") == "abc-123"

    def test_company_home_prefix_is_dropped(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["relativePath"] = request.url.params.get("relativePath")
            return httpx.Response(200, json={"entry": {"id": "found-id"}})

        client = self._client(handler)
        assert client.resolve_node_id("/Company Home/Engineering/spec.pdf") == "found-id"
        assert seen["relativePath"] == "Engineering/spec.pdf"

    def test_empty_reference_is_rejected(self):
        client = self._client(lambda request: httpx.Response(500))
        with pytest.raises(AlfrescoError):
            client.resolve_node_id("")


class TestFindFolders:
    def _client(self, handler):
        client = AlfrescoClient("http://acs.test:8080")
        client._client = httpx.Client(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        client._query_language = "afts"
        return client

    def test_search_is_exact_and_returns_acl_visible_folder_paths(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["query"] = json.loads(request.content)["query"]["query"]
            return httpx.Response(
                200,
                json={
                    "list": {
                        "entries": [
                            {
                                "entry": {
                                    "id": "f-1",
                                    "name": "Shared",
                                    "nodeType": "cm:folder",
                                    "path": {"name": "/Company Home/Shared"},
                                }
                            },
                            {
                                "entry": {
                                    "id": "c-1",
                                    "name": "Shared",
                                    "nodeType": "cm:content",
                                    "path": {"name": "/Company Home/Shared"},
                                }
                            },
                        ]
                    }
                },
            )

        client = self._client(handler)
        assert client.find_folders("Shared") == [
            {"id": "f-1", "name": "Shared", "path": "/Company Home/Shared"}
        ]
        assert seen["query"] == 'TYPE:"cm:folder" AND =cm:name:"Shared"'

    def test_rejects_quoted_folder_names_before_search(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("unsafe name must not reach AFTS")

        with pytest.raises(AlfrescoError, match="no quotes"):
            self._client(handler).find_folders('Shared" OR *')


class TestCreateFolderProperties:
    def test_description_and_title_are_sent_as_cm_properties(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"entry": {"id": "f-1"}})

        client = AlfrescoClient("http://acs.test:8080")
        client._client = httpx.Client(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )

        client.create_folder("parent-1", "Q4 Reports", "Q4 Reports", "Quarterly reports")

        assert seen["body"]["properties"] == {
            "cm:title": "Q4 Reports",
            "cm:description": "Quarterly reports",
        }


class TestFolderRules:
    def _client(self, handler):
        client = AlfrescoClient("http://acs.test:8080")
        client._client = httpx.Client(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        return client

    def test_lists_rules_from_the_default_rule_set(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["maxItems"] = request.url.params["maxItems"]
            return httpx.Response(
                200,
                json={"list": {"entries": [{"entry": {"id": "rule-1", "name": "Tag PDFs"}}]}},
            )

        client = self._client(handler)

        assert client.folder_rules("folder-1") == [{"id": "rule-1", "name": "Tag PDFs"}]
        assert seen == {
            "path": (
                "/alfresco/api/-default-/public/alfresco/versions/1/nodes/folder-1/"
                "rule-sets/-default-/rules"
            ),
            "maxItems": "100",
        }

    def test_missing_default_rule_set_is_an_empty_list(self):
        client = self._client(
            lambda _request: httpx.Response(404, json={"error": {"briefSummary": "not found"}})
        )

        assert client.folder_rules("folder-1") == []

    def test_creates_a_rule_with_the_given_trigger_condition_and_action(self):
        seen = {}
        rule = {
            "name": "Tag incoming PDFs",
            "isEnabled": True,
            "isInheritable": False,
            "triggers": ["inbound"],
            "conditions": {
                "booleanMode": "and",
                "inverted": False,
                "simpleConditions": [
                    {"field": "cm:name", "comparator": "ends", "parameter": ".pdf"}
                ],
            },
            "actions": [
                {"actionDefinitionId": "add-features", "params": {"aspect-name": "cm:versionable"}}
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"entry": {"id": "rule-1", **rule}})

        client = self._client(handler)

        created = client.create_folder_rule("folder-1", rule)

        assert created["id"] == "rule-1"
        assert seen["path"].endswith("/nodes/folder-1/rule-sets/-default-/rules")
        assert seen["body"] == rule


class TestTransportResilience:
    def test_unreachable_host_is_wrapped(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = AlfrescoClient("http://acs.test:8080")
        client._client = httpx.Client(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        with pytest.raises(AlfrescoError) as excinfo:
            client.get_current_user()
        assert "cannot reach" in str(excinfo.value)

    def test_expired_ticket_triggers_one_reauth_and_replay(self):
        calls = {"people": 0, "tickets": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tickets"):
                calls["tickets"] += 1
                return httpx.Response(201, json={"entry": {"id": "TICKET_new"}})
            calls["people"] += 1
            if calls["people"] == 1:
                return httpx.Response(401, json={"error": {"briefSummary": "expired"}})
            return httpx.Response(200, json={"entry": {"id": "admin"}})

        client = AlfrescoClient("http://acs.test:8080")
        client._client = httpx.Client(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        client.username, client._password = "admin", "admin"

        assert client.get_current_user()["id"] == "admin"
        assert calls["tickets"] == 1
        assert calls["people"] == 2

    def test_empty_query_is_rejected_before_the_network(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not reach the repository")

        client = AlfrescoClient("http://acs.test:8080")
        client._client = httpx.Client(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        with pytest.raises(AlfrescoError):
            client.search_afts("   ")
