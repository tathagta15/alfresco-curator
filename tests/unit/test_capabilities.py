"""The repository publishes what it can do, so the agent is not limited to
the tools somebody remembered to write."""

from __future__ import annotations

from unittest.mock import Mock, patch

from alfresco_curator.agent.actions import MUTATING_TOOLS, AuditLog
from alfresco_curator.agent.capabilities import (
    MUTATING_CAPABILITIES,
    build_capability_tools,
)
from alfresco_curator.agent.deep import build_deep_agent
from alfresco_curator.errors import AlfrescoError


def client() -> Mock:
    stub = Mock()
    stub.username = "admin"
    stub.detect_query_language.return_value = "afts"
    stub.action_definitions.return_value = [
        {"id": "simple-workflow", "title": "Add simple workflow", "description": "d"},
        {"id": "counter", "title": "Increment Counter", "description": "d"},
    ]
    stub.execute_action.return_value = {"id": "exec-1"}
    return stub


def tools_for(stub: Mock, audit: AuditLog) -> dict:
    return {tool.name: tool for tool in build_capability_tools(stub, audit)}


class TestDiscovery:
    def test_the_catalogue_is_read_from_the_repository(self):
        stub = client()
        result = tools_for(stub, AuditLog())["list_actions"].invoke({})
        assert [a["id"] for a in result["result"]] == ["simple-workflow", "counter"]

    def test_a_node_narrows_the_catalogue_to_what_applies(self):
        stub = client()
        stub.actions_for_node.return_value = [{"id": "check-out", "title": "Check out"}]
        tools_for(stub, AuditLog())["list_actions"].invoke({"node_id": "n-1"})
        stub.actions_for_node.assert_called_once_with("n-1")
        stub.action_definitions.assert_not_called()

    def test_discovery_is_not_audited_because_it_changes_nothing(self):
        audit = AuditLog()
        tools_for(client(), audit)["list_actions"].invoke({})
        assert audit.entries == []


class TestRunAction:
    def test_an_arbitrary_action_can_be_executed(self):
        stub = client()
        result = tools_for(stub, AuditLog())["run_action"].invoke(
            {"action_id": "counter", "node_id": "n-1", "params": {"property": "cm:counter"}}
        )
        assert result["ok"] is True
        stub.execute_action.assert_called_once_with("counter", "n-1", {"property": "cm:counter"})

    def test_the_caller_is_told_it_is_only_queued(self):
        # Verified live: add-features took ~1.5s to apply after a 200 reply.
        result = tools_for(client(), AuditLog())["run_action"].invoke(
            {"action_id": "add-features", "node_id": "n-1"}
        )
        assert "asynchronous" in result["note"]

    def test_params_sent_as_a_json_string_are_accepted(self):
        # Observed live: the model serialised params rather than nesting them.
        stub = client()
        tools_for(stub, AuditLog())["run_action"].invoke(
            {
                "action_id": "add-features",
                "node_id": "n-1",
                "params": '{"aspect-name": "cm:versionable"}',
            }
        )
        stub.execute_action.assert_called_once_with(
            "add-features", "n-1", {"aspect-name": "cm:versionable"}
        )

    def test_unparseable_params_are_reported_not_crashed(self):
        result = tools_for(client(), AuditLog())["run_action"].invoke(
            {"action_id": "counter", "node_id": "n-1", "params": "{not json"}
        )
        assert result["ok"] is False and "valid JSON" in result["error"]

    def test_empty_params_are_treated_as_none(self):
        stub = client()
        tools_for(stub, AuditLog())["run_action"].invoke(
            {"action_id": "check-out", "node_id": "n-1", "params": ""}
        )
        stub.execute_action.assert_called_once_with("check-out", "n-1", {})

    def test_a_refused_action_is_audited_not_raised(self):
        stub = client()
        stub.execute_action.side_effect = AlfrescoError(403, "Permission was denied")
        audit = AuditLog()
        result = tools_for(stub, audit)["run_action"].invoke(
            {"action_id": "script", "node_id": "n-1"}
        )
        assert result["ok"] is False
        assert audit.entries[0].outcome == "failed"


class TestApprovalCoversCapabilities:
    def test_every_mutating_capability_is_held_for_approval(self):
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client(), audit=AuditLog(), allow_writes=True)
        held = set(create.call_args.kwargs["interrupt_on"])
        assert set(MUTATING_CAPABILITIES) <= held
        assert set(MUTATING_TOOLS) <= held

    def test_running_an_arbitrary_action_needs_approval(self):
        # run_action can reach anything, so it must never be unattended.
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client(), audit=AuditLog(), allow_writes=True)
        assert create.call_args.kwargs["interrupt_on"]["run_action"] is True

    def test_discovery_is_never_interrupted(self):
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client(), audit=AuditLog(), allow_writes=True)
        held = create.call_args.kwargs["interrupt_on"]
        assert "list_actions" not in held and "describe_action" not in held

    def test_one_ledger_covers_both_tool_families(self):
        audit = AuditLog()
        with patch("alfresco_curator.agent.deep.create_deep_agent"):
            build_deep_agent(client(), audit=audit, allow_writes=True)
        # A split audit would mean two partial records of one run.
        assert audit.entries == []
