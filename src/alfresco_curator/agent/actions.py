"""Write actions, and the record of them.

Searching is safe to do on a hunch; moving a thousand documents is not. Every
function here mutates the repository as the signed-in user, so the repository
enforces that user's permissions rather than anything the agent believes - and
every call is recorded before and after, so a run can be audited even when it
half-fails.

The tools are deliberately small and literal. A tool that "organises a folder"
hides what it did; `move_node` called forty times does not.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool

from alfresco_curator import governance
from alfresco_curator.agent.guardrails import MutationBudget
from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.errors import AlfrescoError

log = logging.getLogger(__name__)

# Tools that change the repository. The agent is interrupted before each one,
# so a human approves the specific call with its arguments visible.
MUTATING_TOOLS = (
    "create_folder",
    "rename_node",
    "set_properties",
    "add_aspects",
    "add_tag",
    "move_node",
    "copy_node",
    "delete_node",
    "grant_permission",
)


@dataclass(slots=True)
class AuditEntry:
    action: str
    arguments: dict[str, Any]
    user: str
    at: float
    outcome: str = "pending"
    detail: str = ""
    # What the node looked like before, captured at call time. Without it a
    # rename or a move cannot be put back.
    before: dict[str, Any] = field(default_factory=dict)
    created_id: str = ""
    undone: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "arguments": self.arguments,
            "user": self.user,
            "at": self.at,
            "outcome": self.outcome,
            "detail": self.detail,
            "undone": self.undone,
            "reversible": self.action in REVERSIBLE,
        }


# What a successful call can be put back.
REVERSIBLE = frozenset(
    {
        "create_folder",
        "rename_node",
        "move_node",
        "copy_node",
        "add_tag",
        "add_aspects",
        "set_properties",
        "delete_node",
    }
)

# Mutations with no inverse, and why. Stated rather than left implied: undo
# already reports anything outside REVERSIBLE, so an omission here looks
# exactly like a deliberate decision, which is how a change that cannot be
# put back ends up being presented as one that can.
IRREVERSIBLE: dict[str, str] = {
    # The repository can run anything, including scripts, so no general
    # inverse exists and pretending otherwise would be worse than refusing.
    "run_action": "a repository action may do anything, so it has no inverse",
    # Reverting an ACL needs the prior one, and `snapshot` captures name,
    # parent, aspects and properties - not permissions. Restoring a guess
    # would be worse than saying so.
    "grant_permission": "the previous permissions are not captured, so they cannot be restored",
}


@dataclass(slots=True)
class AuditLog:
    """An ordered record of every attempted change in one session."""

    entries: list[AuditEntry] = field(default_factory=list)
    budget: MutationBudget = field(default_factory=MutationBudget)

    def record(self, action: str, arguments: dict[str, Any], user: str) -> AuditEntry:
        entry = AuditEntry(action=action, arguments=arguments, user=user, at=time.time())
        self.entries.append(entry)
        return entry

    def as_list(self) -> list[dict[str, Any]]:
        return [entry.as_dict() for entry in self.entries]

    @property
    def changed(self) -> int:
        return sum(1 for entry in self.entries if entry.outcome == "ok")

    @property
    def undoable(self) -> list[AuditEntry]:
        return [
            entry
            for entry in self.entries
            if entry.outcome == "ok" and not entry.undone and entry.action in REVERSIBLE
        ]


def _ledger(entry: AuditEntry) -> None:
    """Mirror one settled audit entry into the durable ledger.

    The in-session log answers "can this be undone"; it dies with the session.
    The ledger answers "what did the agent change, on whose instruction", and
    has to outlive the container for that to be worth anything.
    """
    governance.record_decision(
        entry.action,
        entry.arguments,
        entry.user,
        entry.outcome,
        reversible=entry.action in REVERSIBLE,
        detail=entry.detail,
    )


def build_action_tools(client: AlfrescoClient, audit: AuditLog) -> list[StructuredTool]:
    """Write tools bound to one authenticated session, each audited."""

    def snapshot(node_id: str) -> dict[str, Any]:
        """The fields an inverse needs, read before the change is made."""
        try:
            node = client.get_node_details(node_id)
        except AlfrescoError:
            return {}
        return {
            "name": node.get("name"),
            "parentId": node.get("parentId"),
            "aspectNames": list(node.get("aspectNames") or []),
            "properties": dict(node.get("properties") or {}),
        }

    def audited(
        name: str, call, arguments: dict[str, Any], before: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # Charged before the call, so a refusal costs nothing and the limit
        # cannot be walked past by a tool that fails and retries.
        over = audit.budget.take(name)
        if over:
            log.warning("mutation budget exhausted, refusing %s", name)
            return {"ok": False, "error": over}

        entry = audit.record(name, arguments, client.username or "unknown")
        entry.before = before or {}
        try:
            result = call()
        except AlfrescoError as exc:
            entry.outcome, entry.detail = "failed", str(exc)
            log.info("action %s failed: %s", name, exc)
            _ledger(entry)
            # Returned rather than raised: the agent should report the refusal
            # and carry on with the rest of a batch.
            return {"ok": False, "error": str(exc)}
        entry.outcome = "ok"
        entry.detail = result.get("id", "") if isinstance(result, dict) else ""
        entry.created_id = entry.detail
        _ledger(entry)
        return {"ok": True, "result": _thin(result)}

    def create_folder(parent_id: str, name: str, title: str = "") -> dict[str, Any]:
        return audited(
            "create_folder",
            lambda: client.create_folder(parent_id, name, title or None),
            {"parent_id": parent_id, "name": name, "title": title},
        )

    def rename_node(node_id: str, new_name: str) -> dict[str, Any]:
        return audited(
            "rename_node",
            lambda: client.update_node(node_id, name=new_name),
            {"node_id": node_id, "new_name": new_name},
            snapshot(node_id),
        )

    def set_properties(node_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return audited(
            "set_properties",
            lambda: client.update_node(node_id, properties=properties),
            {"node_id": node_id, "properties": properties},
            snapshot(node_id),
        )

    def add_aspects(node_id: str, aspects: list[str]) -> dict[str, Any]:
        return audited(
            "add_aspects",
            lambda: client.add_aspects(node_id, aspects),
            {"node_id": node_id, "aspects": aspects},
            snapshot(node_id),
        )

    def add_tag(node_id: str, tag: str) -> dict[str, Any]:
        return audited(
            "add_tag", lambda: client.add_tag(node_id, tag), {"node_id": node_id, "tag": tag}
        )

    def move_node(node_id: str, target_parent_id: str) -> dict[str, Any]:
        return audited(
            "move_node",
            lambda: client.move_node(node_id, target_parent_id),
            {"node_id": node_id, "target_parent_id": target_parent_id},
            snapshot(node_id),
        )

    def copy_node(node_id: str, target_parent_id: str, new_name: str = "") -> dict[str, Any]:
        return audited(
            "copy_node",
            lambda: client.copy_node(node_id, target_parent_id, new_name or None),
            {"node_id": node_id, "target_parent_id": target_parent_id, "new_name": new_name},
        )

    def delete_node(node_id: str) -> dict[str, Any]:
        # Trashcan, never permanent: a wrong call must stay recoverable.
        return audited(
            "delete_node",
            lambda: (client.delete_node(node_id), {"id": node_id})[1],
            {"node_id": node_id},
        )

    def grant_permission(node_id: str, authority: str, role: str) -> dict[str, Any]:
        return audited(
            "grant_permission",
            lambda: client.set_permission(node_id, authority, role),
            {"node_id": node_id, "authority": authority, "role": role},
        )

    def list_children(node_id: str) -> dict[str, Any]:
        """Reading children is safe, but it is how a bulk plan gets built."""
        try:
            children = client.list_children(node_id)
        except AlfrescoError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "children": [_thin(child) for child in children]}

    specs = [
        (create_folder, "create_folder", "Create a folder under a parent node."),
        (rename_node, "rename_node", "Rename one node."),
        (
            set_properties,
            "set_properties",
            "Set properties on a node, e.g. {'cm:title': 'x', 'cm:description': 'y'}.",
        ),
        (
            add_aspects,
            "add_aspects",
            "Add aspects to a node, e.g. ['cm:titled']. Existing aspects are kept.",
        ),
        (add_tag, "add_tag", "Add a tag to a node."),
        (move_node, "move_node", "Move a node into another folder."),
        (copy_node, "copy_node", "Copy a node into another folder."),
        (delete_node, "delete_node", "Move a node to the trashcan. Recoverable, not permanent."),
        (
            grant_permission,
            "grant_permission",
            "Grant an authority a role on a node, e.g. authority 'GROUP_FINANCE', role 'Consumer'.",
        ),
        (list_children, "list_children", "List the children of a folder. Read-only."),
    ]
    return [
        StructuredTool.from_function(func=func, name=name, description=description)
        for func, name, description in specs
    ]


def _thin(node: dict[str, Any]) -> dict[str, Any]:
    """Only the fields worth spending context on."""
    if not isinstance(node, dict):
        return {}
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "nodeType": node.get("nodeType"),
        "path": (node.get("path") or {}).get("name"),
        "isFolder": node.get("isFolder"),
    }


def undo_entry(client: AlfrescoClient, entry: AuditEntry) -> None:
    """Put one change back, or raise if it cannot be.

    Reversal runs as the same user, so a change they were allowed to make is
    one they are allowed to unmake.
    """
    action, args, before = entry.action, entry.arguments, entry.before

    if action in ("create_folder", "copy_node"):
        client.delete_node(entry.created_id, permanent=True)
    elif action == "rename_node":
        client.update_node(args["node_id"], name=before["name"])
    elif action == "move_node":
        client.move_node(args["node_id"], before["parentId"])
    elif action == "add_tag":
        client.remove_tag(args["node_id"], args["tag"])
    elif action == "add_aspects":
        client.update_node(args["node_id"], aspects=before.get("aspectNames", []))
    elif action == "set_properties":
        # Only the keys this change touched are restored; a null clears one
        # that did not exist before.
        previous = before.get("properties", {})
        client.update_node(
            args["node_id"],
            properties={key: previous.get(key) for key in args["properties"]},
        )
    elif action == "delete_node":
        client.restore_node(args["node_id"])
    else:
        raise AlfrescoError(400, f"{action} cannot be reversed")


def undo_run(client: AlfrescoClient, audit: AuditLog) -> dict[str, Any]:
    """Reverse this session's changes, newest first.

    Order matters: a folder created and then filled has to be emptied before
    it can go, and a rename after a move must be undone before the move.
    """
    reversed_entries: list[dict[str, Any]] = []
    for entry in reversed(audit.undoable):
        try:
            undo_entry(client, entry)
        except AlfrescoError as exc:
            reversed_entries.append({"action": entry.action, "ok": False, "error": str(exc)})
            continue
        entry.undone = True
        reversed_entries.append({"action": entry.action, "ok": True})

    irreversible = [
        entry.action
        for entry in audit.entries
        if entry.outcome == "ok" and entry.action not in REVERSIBLE
    ]
    return {
        "reversed": sum(1 for row in reversed_entries if row["ok"]),
        "failed": [row for row in reversed_entries if not row["ok"]],
        "irreversible": sorted(set(irreversible)),
    }
