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
from alfresco_curator.agent.guardrails import MutationBudget
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
    def test_folder_rules_are_readable_with_the_signed_in_user(self):
        stub = client()
        stub.resolve_node_id.return_value = "folder-1"
        stub.get_node_details.return_value = {
            "id": "folder-1",
            "name": "Finance",
            "nodeType": "cm:folder",
            "isFolder": True,
        }
        stub.folder_rules.return_value = [{"id": "rule-1", "name": "Tag PDFs"}]

        result = tools_for(stub, AuditLog())["list_folder_rules"].invoke({"folder": "folder-1"})

        assert result["ok"] is True
        assert result["result"]["rules"] == [{"id": "rule-1", "name": "Tag PDFs"}]
        stub.folder_rules.assert_called_once_with("folder-1")

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

    def test_folder_names_resolve_to_exact_acl_visible_paths(self):
        stub = client()
        stub.find_folders.return_value = [
            {"id": "folder-1", "name": "Shared", "path": "/Company Home/Shared"}
        ]

        result = tools_for(stub, AuditLog())["find_folders"].invoke({"name": "Shared"})

        assert result["result"]["matches"] == [
            {"id": "folder-1", "name": "Shared", "path": "/Company Home/Shared"}
        ]
        stub.find_folders.assert_called_once_with("Shared")

    def test_folder_name_query_rejects_quotes(self):
        stub = client()
        stub.find_folders.side_effect = AlfrescoError(400, "unsafe folder name")
        result = tools_for(stub, AuditLog())["find_folders"].invoke({"name": 'Shared" OR *'})

        assert result["ok"] is False
        stub.find_folders.assert_called_once_with('Shared" OR *')


class TestRunAction:
    def test_folder_rules_follow_the_repository_action_policy(self, monkeypatch):
        monkeypatch.setattr(
            "alfresco_curator.agent.capabilities.governance.change_denial",
            lambda name: (
                "Alfresco repository actions are disabled" if name == "create_folder_rule" else ""
            ),
        )

        names = {tool.name for tool in build_capability_tools(client(), AuditLog())}

        assert "create_folder_rule" not in names
        assert "list_folder_rules" in names

    def test_create_folder_rule_uses_resolved_folder_and_acs_payload(self):
        stub = client()
        stub.resolve_node_id.return_value = "folder-1"
        stub.get_node_details.return_value = {
            "id": "folder-1",
            "name": "Invoices",
            "nodeType": "cm:folder",
            "isFolder": True,
        }
        stub.actions_for_node.return_value = [{"id": "add-features"}]
        stub.action_definition.return_value = {"id": "add-features"}
        stub.create_folder_rule.return_value = {"id": "rule-1", "name": "Version PDFs"}

        result = tools_for(stub, AuditLog())["create_folder_rule"].invoke(
            {
                "folder": "folder-1",
                "name": "Version PDFs",
                "action_id": "add-features",
                "trigger": "inbound",
                "params": {"aspect-name": "cm:versionable"},
                "conditions": [{"field": "cm:name", "comparator": "ends", "parameter": ".pdf"}],
            }
        )

        assert result["ok"] is True
        stub.actions_for_node.assert_called_once_with("folder-1")
        stub.create_folder_rule.assert_called_once_with(
            "folder-1",
            {
                "name": "Version PDFs",
                "description": "",
                "isEnabled": True,
                "isInheritable": False,
                "triggers": ["inbound"],
                "actions": [
                    {
                        "actionDefinitionId": "add-features",
                        "params": {"aspect-name": "cm:versionable"},
                    }
                ],
                "conditions": {
                    "booleanMode": "and",
                    "inverted": False,
                    "compositeConditions": [],
                    "simpleConditions": [
                        {"field": "cm:name", "comparator": "ends", "parameter": ".pdf"}
                    ],
                },
            },
        )

    def test_folder_rule_refuses_ambiguous_folder_names(self):
        stub = client()
        stub.resolve_node_id.return_value = "Shared"
        stub.get_node_details.side_effect = AlfrescoError(404, "not found")
        stub.find_folders.return_value = [
            {"id": "f-1", "path": "/Company Home/Shared"},
            {"id": "f-2", "path": "/Sites/Finance/documentLibrary/Shared"},
        ]

        result = tools_for(stub, AuditLog())["create_folder_rule"].invoke(
            {
                "folder": "Shared",
                "name": "Version PDFs",
                "action_id": "add-features",
                "conditions": [{"field": "cm:name", "comparator": "ends", "parameter": ".pdf"}],
            }
        )

        assert result["ok"] is False
        assert "Several accessible folders" in result["error"]
        stub.create_folder_rule.assert_not_called()

    def test_folder_rule_rejects_an_action_unavailable_for_the_folder(self):
        stub = client()
        stub.resolve_node_id.return_value = "folder-1"
        stub.get_node_details.return_value = {"nodeType": "cm:folder", "isFolder": True}
        stub.actions_for_node.return_value = []

        result = tools_for(stub, AuditLog())["create_folder_rule"].invoke(
            {
                "folder": "folder-1",
                "name": "Version PDFs",
                "action_id": "add-features",
                "apply_to_all": True,
            }
        )

        assert result["ok"] is False
        assert "not available for folder" in result["error"]
        stub.action_definition.assert_not_called()
        stub.create_folder_rule.assert_not_called()

    def test_folder_rule_requires_conditions_or_explicit_apply_to_all(self):
        stub = client()

        result = tools_for(stub, AuditLog())["create_folder_rule"].invoke(
            {"folder": "folder-1", "name": "All items", "action_id": "add-features"}
        )

        assert result["ok"] is False
        assert "apply_to_all=true" in result["error"]
        stub.create_folder_rule.assert_not_called()

    def test_disabled_sharing_tools_are_hidden_but_read_only_discovery_remains(self, monkeypatch):
        monkeypatch.setattr(
            "alfresco_curator.agent.capabilities.governance.change_denial",
            lambda name: "sharing is disabled" if name == "create_shared_link" else "",
        )

        names = {tool.name for tool in build_capability_tools(client(), AuditLog())}

        assert "create_shared_link" not in names
        assert "list_actions" in names
        assert "run_action" in names

    def test_stale_agent_cannot_run_a_disabled_repository_action(self, monkeypatch):
        stub = client()
        tools = tools_for(stub, AuditLog())
        monkeypatch.setattr(
            "alfresco_curator.agent.capabilities.governance.change_denial",
            lambda name: "custom actions are disabled" if name == "run_action" else "",
        )

        result = tools["run_action"].invoke(
            {"action_id": "counter", "node_id": "n-1", "params": {}}
        )

        assert result == {"ok": False, "error": "custom actions are disabled"}
        stub.execute_action.assert_not_called()

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

    def test_native_action_spends_the_governance_mutation_budget(self, monkeypatch):
        monkeypatch.setattr(
            "alfresco_curator.agent.capabilities.governance.effective_policy",
            lambda: {"mutation_budget": 0},
        )
        stub = client()
        audit = AuditLog(budget=MutationBudget(limit=0))

        result = tools_for(stub, audit)["run_action"].invoke(
            {"action_id": "counter", "node_id": "n-1", "params": {}}
        )

        assert result["ok"] is False
        assert "limit of 0" in result["error"]
        stub.execute_action.assert_not_called()

    def test_repository_read_only_state_blocks_native_action_retries(self):
        stub = client()
        stub.execute_action.side_effect = AlfrescoError(
            403, "09260168 Access Denied. The system is currently in read-only mode."
        )
        audit = AuditLog()
        tools = tools_for(stub, audit)

        first = tools["run_action"].invoke({"action_id": "workflow", "node_id": "n-1"})
        second = tools["create_site"].invoke({"site_id": "q4", "title": "Q4"})

        assert first["ok"] is False and "licence" in first["error"].lower()
        assert second["ok"] is False and "read-only mode" in second["error"].lower()
        stub.create_site.assert_not_called()


class TestApprovalCoversCapabilities:
    def test_global_pause_removes_mutations_and_their_approval_interrupts(self, monkeypatch):
        monkeypatch.setattr(
            "alfresco_curator.agent.deep.effective_policy",
            lambda: {"allow_repository_changes": False},
        )
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client(), audit=AuditLog(), allow_writes=True)

        names = {tool.name for tool in create.call_args.kwargs["tools"]}
        assert not (set(MUTATING_TOOLS) | set(MUTATING_CAPABILITIES)) & names
        assert {"list_actions", "describe_action", "find_folders"} <= names
        assert create.call_args.kwargs["interrupt_on"] is None

    def test_access_gate_hides_permission_tool_and_access_capabilities(self, monkeypatch):
        def denial(name):
            if name in {"grant_permission", "create_shared_link", "create_group"}:
                return "access changes are disabled"
            return ""

        monkeypatch.setattr(
            "alfresco_curator.agent.deep.change_denial",
            denial,
        )
        monkeypatch.setattr(
            "alfresco_curator.agent.capabilities.governance.change_denial",
            denial,
        )
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client(), audit=AuditLog(), allow_writes=True)

        names = {tool.name for tool in create.call_args.kwargs["tools"]}
        assert "grant_permission" not in names
        assert "create_shared_link" not in names
        assert "create_group" not in names
        assert "create_folder" in names
        assert "list_actions" in names

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
        assert "find_folders" not in held

    def test_one_ledger_covers_both_tool_families(self):
        audit = AuditLog()
        with patch("alfresco_curator.agent.deep.create_deep_agent"):
            build_deep_agent(client(), audit=audit, allow_writes=True)
        # A split audit would mean two partial records of one run.
        assert audit.entries == []
