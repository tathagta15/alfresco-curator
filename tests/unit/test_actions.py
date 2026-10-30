"""Changes are proposed, audited, and bounded by the user's permissions."""

from __future__ import annotations

from unittest.mock import Mock, patch

from alfresco_curator import governance
from alfresco_curator.agent.actions import (
    MUTATING_TOOLS,
    AuditLog,
    build_action_tools,
)
from alfresco_curator.agent.capabilities import MUTATING_CAPABILITIES
from alfresco_curator.agent.deep import build_deep_agent
from alfresco_curator.errors import AlfrescoError


def client(**overrides) -> Mock:
    stub = Mock()
    stub.username = "demo.analyst"
    stub.detect_query_language.return_value = "afts"
    # Mirrors the real client: every write snapshots the node first, so this
    # has to be a dict rather than a bare Mock.
    stub.get_node_details.return_value = {
        "name": "original-name",
        "nodeType": "cm:folder",
        "isFolder": True,
        "parentId": "home",
        "aspectNames": ["cm:titled"],
        "properties": {"cm:title": "Original title"},
    }
    stub.create_folder.return_value = {"id": "new-1", "name": "Invoices", "isFolder": True}
    stub.update_node.return_value = {"id": "n-1", "name": "renamed"}
    stub.move_node.return_value = {"id": "n-1", "name": "moved"}
    for key, value in overrides.items():
        getattr(stub, key).side_effect = value
    return stub


def tools_for(stub: Mock, audit: AuditLog) -> dict:
    return {tool.name: tool for tool in build_action_tools(stub, audit)}


class TestAuditTrail:
    def test_global_pause_blocks_a_write_and_records_the_refusal(self, monkeypatch):
        monkeypatch.setattr(
            governance,
            "change_denial",
            lambda _action: "Repository changes are paused by governance policy.",
        )
        stub = client()
        audit = AuditLog()

        result = tools_for(stub, audit)["create_folder"].invoke(
            {"parent_id": "parent-1", "name": "Q4 Reports"}
        )

        assert result == {
            "ok": False,
            "error": "Repository changes are paused by governance policy.",
        }
        assert audit.entries[0].outcome == "failed"
        stub.create_folder.assert_not_called()

    def test_folder_description_is_forwarded_as_audited_action_data(self):
        stub = client()
        stub.resolve_node_id.return_value = "parent-1"
        audit = AuditLog()
        tools_for(stub, audit)["create_folder"].invoke(
            {
                "parent_id": "parent-1",
                "name": "Q4 Reports",
                "title": "Q4 Reports",
                "description": "Quarterly reports",
            }
        )

        stub.create_folder.assert_called_once_with(
            "parent-1", "Q4 Reports", "Q4 Reports", "Quarterly reports"
        )
        assert audit.entries[0].arguments["description"] == "Quarterly reports"

    def test_repository_read_only_error_stops_following_writes(self):
        stub = client(
            update_node=AlfrescoError(
                403, "09260168 Access Denied. The system is currently in read-only mode."
            )
        )
        audit = AuditLog()
        tools = tools_for(stub, audit)

        first = tools["rename_node"].invoke({"node_id": "n-1", "new_name": "Q4 final"})
        second = tools["add_tag"].invoke({"node_id": "n-1", "tag": "reviewed"})

        assert first["ok"] is False and "licence" in first["error"].lower()
        assert second["ok"] is False and "read-only mode" in second["error"].lower()
        stub.add_tag.assert_not_called()
        assert audit.write_blocked_reason == first["error"]

    def test_node_acl_denial_does_not_block_other_authorized_writes(self):
        stub = client(update_node=AlfrescoError(403, "Permission was denied"))
        audit = AuditLog()
        tools = tools_for(stub, audit)

        denied = tools["rename_node"].invoke({"node_id": "restricted", "new_name": "Q4 final"})
        allowed = tools["add_tag"].invoke({"node_id": "visible", "tag": "reviewed"})

        assert denied["ok"] is False
        assert "licence" not in denied["error"].lower()
        assert allowed["ok"] is True
        assert not audit.write_blocked_reason
        stub.add_tag.assert_called_once_with("visible", "reviewed")

    def test_folder_creation_refuses_an_unresolved_parent_name(self):
        stub = client(get_node_details=AlfrescoError(404, "not found"))
        stub.find_folders.return_value = []
        audit = AuditLog()

        result = tools_for(stub, audit)["create_folder"].invoke(
            {"parent_id": "Shared", "name": "Q4 Reports"}
        )

        assert result["ok"] is False
        assert "full path" in result["error"]
        stub.create_folder.assert_not_called()

    def test_folder_creation_refuses_an_ambiguous_parent_name(self):
        stub = client(get_node_details=AlfrescoError(404, "not found"))
        stub.find_folders.return_value = [
            {"id": "folder-1", "path": "/Company Home/Shared"},
            {"id": "folder-2", "path": "/Sites/Finance/documentLibrary/Shared"},
        ]

        result = tools_for(stub, AuditLog())["create_folder"].invoke(
            {"parent_id": "Shared", "name": "Q4 Reports"}
        )

        assert result["ok"] is False
        assert "/Company Home/Shared" in result["error"]
        stub.create_folder.assert_not_called()

    def test_policy_budget_change_applies_to_an_existing_session(self, monkeypatch):
        limits = iter([2, 1])
        monkeypatch.setattr(
            governance,
            "effective_policy",
            lambda: {"mutation_budget": next(limits)},
        )
        audit = AuditLog()
        tool = tools_for(client(), audit)["create_folder"]

        first = tool.invoke({"parent_id": "p-1", "name": "first"})
        second = tool.invoke({"parent_id": "p-1", "name": "second"})

        assert first["ok"] is True
        assert second["ok"] is False
        assert "limit of 1" in second["error"]

    def test_a_successful_change_is_recorded_against_the_user(self):
        audit = AuditLog()
        tools_for(client(), audit)["create_folder"].invoke({"parent_id": "p-1", "name": "Invoices"})
        entry = audit.entries[0]
        assert (entry.action, entry.outcome, entry.user) == (
            "create_folder",
            "ok",
            "demo.analyst",
        )
        assert entry.arguments["name"] == "Invoices"

    def test_a_refused_change_is_recorded_too(self):
        # An audit that only lists successes hides exactly what matters.
        audit = AuditLog()
        stub = client(update_node=AlfrescoError(403, "Permission was denied"))
        result = tools_for(stub, audit)["rename_node"].invoke({"node_id": "n-1", "new_name": "x"})
        assert result["ok"] is False
        assert audit.entries[0].outcome == "failed"
        assert "denied" in audit.entries[0].detail

    def test_a_refusal_is_returned_rather_than_raised(self):
        # A batch must survive one refusal and report it.
        stub = client(move_node=AlfrescoError(403, "denied"))
        result = tools_for(stub, AuditLog())["move_node"].invoke(
            {"node_id": "n-1", "target_parent_id": "p-2"}
        )
        assert result == {"ok": False, "error": "HTTP 403: denied"}

    def test_only_successful_changes_are_counted(self):
        audit = AuditLog()
        stub = client(update_node=AlfrescoError(403, "denied"))
        tools = tools_for(stub, audit)
        tools["create_folder"].invoke({"parent_id": "p", "name": "a"})
        tools["rename_node"].invoke({"node_id": "n", "new_name": "b"})
        assert audit.changed == 1


class TestDeletionSafety:
    def test_delete_uses_the_trashcan_not_a_permanent_purge(self):
        stub = client()
        tools_for(stub, AuditLog())["delete_node"].invoke({"node_id": "n-1"})
        stub.delete_node.assert_called_once_with("n-1")
        _, kwargs = stub.delete_node.call_args
        assert kwargs.get("permanent") is not True, "a wrong call must stay recoverable"


class TestAspectMerge:
    def test_existing_aspects_survive_an_addition(self):
        # The REST API replaces aspectNames wholesale, so a naive write drops them.
        stub = Mock()
        stub.username = "admin"
        stub.get_node_details.return_value = {"aspectNames": ["cm:titled", "cm:auditable"]}
        stub.update_node.return_value = {"id": "n-1"}
        build_action_tools(stub, AuditLog())
        from alfresco_curator.alfresco.client import AlfrescoClient

        AlfrescoClient.add_aspects(stub, "n-1", ["cm:versionable"])
        assert stub.update_node.call_args.kwargs["aspects"] == [
            "cm:titled",
            "cm:auditable",
            "cm:versionable",
        ]


class TestApprovalGate:
    def test_every_mutating_tool_is_held_for_approval(self):
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client(), audit=AuditLog(), allow_writes=True)
        # A subset check, because the capability tools are gated here too.
        assert set(MUTATING_TOOLS) <= set(create.call_args.kwargs["interrupt_on"])

    def test_reads_are_not_interrupted(self):
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client(), audit=AuditLog(), allow_writes=True)
        held = create.call_args.kwargs["interrupt_on"]
        assert "search_repository" not in held and "clause_reach" not in held

    def test_a_read_only_agent_offers_no_write_tools(self):
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client())
        names = {tool.name for tool in create.call_args.kwargs["tools"]}
        assert not (names & set(MUTATING_TOOLS))
        assert not (names & set(MUTATING_CAPABILITIES))
        assert {
            "list_actions",
            "describe_action",
            "find_folders",
            "list_versions",
            "list_comments",
        } <= names
        assert create.call_args.kwargs["interrupt_on"] is None

    def test_writes_are_off_unless_asked_for(self):
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client())
        assert "change the repository" not in create.call_args.kwargs["system_prompt"]
