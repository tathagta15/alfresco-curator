"""The probes are exposed as LangChain tools without losing their grounding."""

from __future__ import annotations

from unittest.mock import Mock, patch

from langchain_core.tools import StructuredTool

from alfresco_curator.agent.tools import build_tools


def client_for(language: str = "afts") -> Mock:
    client = Mock()
    client.username = "admin"
    client.detect_query_language.return_value = language
    return client


class TestBuildTools:
    def test_every_probe_is_offered(self):
        names = {tool.name for tool in build_tools(client_for())}
        assert names == {
            "search_repository",
            "clause_reach",
            "path_reach",
            "narrow_conflict",
            "explain_node",
            "trace_pipeline",
        }

    def test_tools_carry_a_schema_a_model_can_call(self):
        tools = {tool.name: tool for tool in build_tools(client_for())}
        assert all(isinstance(tool, StructuredTool) for tool in tools.values())
        assert tools["clause_reach"].args == {"query": {"title": "Query", "type": "string"}}
        assert set(tools["explain_node"].args) == {"query", "node_id"}

    def test_the_detected_language_is_bound_once_not_per_call(self):
        client = client_for("lucene")
        tools = {tool.name: tool for tool in build_tools(client)}
        with patch("alfresco_curator.agent.tools.analyse_empty_result") as probe:
            probe.return_value = []
            tools["clause_reach"].invoke({"query": '+TYPE:"cm:content"'})
            tools["clause_reach"].invoke({"query": '+TEXT:"x"'})
        assert client.detect_query_language.call_count == 1
        assert probe.call_args.kwargs["language"] == "lucene"

    def test_search_returns_the_shape_a_caller_can_read(self):
        client = client_for()
        client.search_afts.return_value = {
            "list": {
                "pagination": {"totalItems": 1},
                "entries": [
                    {"entry": {"id": "n1", "name": "spec.pdf", "path": {"name": "/Company Home"}}}
                ],
            }
        }
        tools = {tool.name: tool for tool in build_tools(client)}
        result = tools["search_repository"].invoke({"query": 'TYPE:"cm:content"'})
        assert result["total"] == 1
        assert result["results"][0]["name"] == "spec.pdf"

    def test_tracing_without_telemetry_reports_rather_than_raises(self):
        tools = {tool.name: tool for tool in build_tools(client_for(), telemetry=None)}
        assert "error" in tools["trace_pipeline"].invoke({"node_id": "n1"})

    def test_a_probe_still_runs_against_the_callers_own_client(self):
        # Permissions are part of the answer, so the tool must not widen scope.
        client = client_for()
        tools = {tool.name: tool for tool in build_tools(client)}
        with patch("alfresco_curator.agent.tools.analyse_path_reach") as probe:
            probe.return_value = []
            tools["path_reach"].invoke({"query": 'PATH:"/app:company_home//*"'})
        assert probe.call_args.args[0] is client
