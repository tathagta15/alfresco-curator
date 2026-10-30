"""A change the agent made must be a change the agent can put back."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from alfresco_curator.agent.actions import (
    REVERSIBLE,
    AuditLog,
    build_action_tools,
    undo_entry,
    undo_run,
)
from alfresco_curator.errors import AlfrescoError


def client() -> Mock:
    stub = Mock()
    stub.username = "admin"
    stub.get_node_details.return_value = {
        "name": "original-name",
        "nodeType": "cm:folder",
        "isFolder": True,
        "parentId": "home",
        "aspectNames": ["cm:titled"],
        "properties": {"cm:title": "Original title"},
    }
    stub.update_node.return_value = {"id": "n-1"}
    stub.move_node.return_value = {"id": "n-1"}
    stub.create_folder.return_value = {"id": "made-1"}
    stub.add_aspects.return_value = {"id": "n-1"}
    stub.add_tag.return_value = {"id": "tag-1"}
    return stub


def tools_for(stub: Mock, audit: AuditLog) -> dict:
    return {tool.name: tool for tool in build_action_tools(stub, audit)}


class TestBeforeState:
    def test_a_rename_records_the_previous_name(self):
        audit = AuditLog()
        tools_for(client(), audit)["rename_node"].invoke({"node_id": "n-1", "new_name": "renamed"})
        assert audit.entries[0].before["name"] == "original-name"

    def test_a_move_records_the_previous_parent(self):
        audit = AuditLog()
        tools_for(client(), audit)["move_node"].invoke(
            {"node_id": "n-1", "target_parent_id": "elsewhere"}
        )
        assert audit.entries[0].before["parentId"] == "home"

    def test_a_refused_change_is_not_offered_for_undo(self):
        audit = AuditLog()
        stub = client()
        stub.update_node.side_effect = AlfrescoError(403, "denied")
        tools_for(stub, audit)["rename_node"].invoke({"node_id": "n-1", "new_name": "x"})
        assert audit.undoable == []


class TestInverse:
    def test_a_created_folder_is_removed(self):
        audit = AuditLog()
        stub = client()
        tools_for(stub, audit)["create_folder"].invoke({"parent_id": "p", "name": "new"})
        undo_entry(stub, audit.entries[0])
        stub.delete_node.assert_called_once_with("made-1", permanent=True)

    def test_a_rename_goes_back_to_the_old_name(self):
        audit = AuditLog()
        stub = client()
        tools_for(stub, audit)["rename_node"].invoke({"node_id": "n-1", "new_name": "renamed"})
        stub.update_node.reset_mock()
        undo_entry(stub, audit.entries[0])
        assert stub.update_node.call_args.kwargs["name"] == "original-name"

    def test_added_aspects_are_rolled_back_to_the_previous_set(self):
        audit = AuditLog()
        stub = client()
        tools_for(stub, audit)["add_aspects"].invoke(
            {"node_id": "n-1", "aspects": ["cm:versionable"]}
        )
        undo_entry(stub, audit.entries[0])
        assert stub.update_node.call_args.kwargs["aspects"] == ["cm:titled"]

    def test_only_the_touched_properties_are_restored(self):
        # Restoring the whole property bag would revert unrelated edits.
        audit = AuditLog()
        stub = client()
        tools_for(stub, audit)["set_properties"].invoke(
            {"node_id": "n-1", "properties": {"cm:title": "new", "cm:description": "new"}}
        )
        undo_entry(stub, audit.entries[0])
        restored = stub.update_node.call_args.kwargs["properties"]
        assert restored == {"cm:title": "Original title", "cm:description": None}

    def test_a_deleted_node_is_restored_from_the_trashcan(self):
        audit = AuditLog()
        stub = client()
        tools_for(stub, audit)["delete_node"].invoke({"node_id": "n-1"})
        undo_entry(stub, audit.entries[0])
        stub.restore_node.assert_called_once_with("n-1")

    def test_an_arbitrary_repository_action_has_no_inverse(self):
        # run_action can execute a script; pretending to reverse it would lie.
        entry = AuditLog().record("run_action", {"action_id": "script"}, "admin")
        with pytest.raises(AlfrescoError, match="cannot be reversed"):
            undo_entry(client(), entry)


class TestUndoRun:
    def test_changes_are_reversed_newest_first(self):
        audit = AuditLog()
        stub = client()
        tools = tools_for(stub, audit)
        tools["create_folder"].invoke({"parent_id": "p", "name": "a"})
        tools["rename_node"].invoke({"node_id": "n-1", "new_name": "b"})

        order: list[str] = []
        stub.update_node.side_effect = lambda *a, **k: order.append("rename") or {"id": "n-1"}
        stub.delete_node.side_effect = lambda *a, **k: order.append("delete")
        undo_run(stub, audit)

        assert order == ["rename", "delete"], "a parent cannot go before its children"

    def test_an_undone_change_is_not_undone_twice(self):
        audit = AuditLog()
        stub = client()
        tools_for(stub, audit)["rename_node"].invoke({"node_id": "n-1", "new_name": "b"})
        undo_run(stub, audit)
        stub.update_node.reset_mock()
        assert undo_run(stub, audit)["reversed"] == 0
        stub.update_node.assert_not_called()

    def test_a_failed_reversal_is_reported_and_does_not_stop_the_rest(self):
        audit = AuditLog()
        stub = client()
        tools = tools_for(stub, audit)
        tools["rename_node"].invoke({"node_id": "n-1", "new_name": "b"})
        tools["create_folder"].invoke({"parent_id": "p", "name": "a"})
        stub.delete_node.side_effect = AlfrescoError(403, "denied")

        outcome = undo_run(stub, audit)
        assert outcome["reversed"] == 1
        assert outcome["failed"][0]["action"] == "create_folder"

    def test_irreversible_actions_are_named_rather_than_hidden(self):
        audit = AuditLog()
        entry = audit.record("run_action", {"action_id": "script"}, "admin")
        entry.outcome = "ok"
        assert undo_run(client(), audit)["irreversible"] == ["run_action"]

    def test_run_action_is_not_claimed_as_reversible(self):
        assert "run_action" not in REVERSIBLE
