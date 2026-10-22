"""Everything else Alfresco can do, including the parts nobody remembers.

Hand-written tools only reach the features whoever wrote them thought of. The
repository already publishes its own catalogue of actions with their parameter
definitions, so `list_actions` and `run_action` reach capabilities this code
has never heard of - simple workflows, counters, type specialisation, image
transforms, scripted actions.

The typed tools below cover what the catalogue does not: versions, the
trashcan, sharing, discussion, groups and sites.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import StructuredTool

from alfresco_curator.agent.actions import AuditLog, _thin
from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.errors import AlfrescoError

log = logging.getLogger(__name__)

# Capability tools that change something, so they join the approval gate.
MUTATING_CAPABILITIES = (
    "run_action",
    "revert_version",
    "lock_node",
    "unlock_node",
    "restore_deleted_node",
    "create_shared_link",
    "add_comment",
    "add_favorite",
    "create_group",
    "add_group_member",
    "create_site",
    "add_site_member",
)


def build_capability_tools(client: AlfrescoClient, audit: AuditLog) -> list[StructuredTool]:
    def audited(name: str, call, arguments: dict[str, Any]) -> dict[str, Any]:
        entry = audit.record(name, arguments, client.username or "unknown")
        try:
            result = call()
        except AlfrescoError as exc:
            entry.outcome, entry.detail = "failed", str(exc)
            return {"ok": False, "error": str(exc)}
        entry.outcome = "ok"
        entry.detail = result.get("id", "") if isinstance(result, dict) else ""
        return {"ok": True, "result": result}

    def read(call) -> dict[str, Any]:
        try:
            return {"ok": True, "result": call()}
        except AlfrescoError as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------- discovery
    def list_actions(node_id: str = "") -> dict[str, Any]:
        """What the repository can do - optionally narrowed to one node."""

        def fetch() -> list[dict[str, Any]]:
            items = client.actions_for_node(node_id) if node_id else client.action_definitions()
            return [
                {"id": a["id"], "title": a.get("title"), "description": a.get("description")}
                for a in items
            ]

        return read(fetch)

    def describe_action(action_id: str) -> dict[str, Any]:
        """The parameters an action takes, so a call can be built correctly."""
        return read(lambda: client.action_definition(action_id))

    def run_action(action_id: str, node_id: str, params: Any = None) -> dict[str, Any]:
        # Models routinely serialise a nested object as a JSON string; that is
        # a transport detail, not a mistake worth failing the call over.
        if isinstance(params, str):
            try:
                params = json.loads(params) if params.strip() else {}
            except json.JSONDecodeError:
                return {"ok": False, "error": f"params is not valid JSON: {params[:120]}"}
        if params is not None and not isinstance(params, dict):
            return {"ok": False, "error": "params must be an object of name/value pairs"}

        result = audited(
            "run_action",
            lambda: client.execute_action(action_id, node_id, params or {}),
            {"action_id": action_id, "node_id": node_id, "params": params or {}},
        )
        if result.get("ok"):
            # Queued, not done: reporting it as complete would be a lie.
            result["note"] = "queued - repository actions run asynchronously"
        return result

    # -------------------------------------------------------------- versions
    def list_versions(node_id: str) -> dict[str, Any]:
        return read(
            lambda: [
                {
                    "id": v["id"],
                    "comment": v.get("versionComment"),
                    "modifiedAt": v.get("modifiedAt"),
                }
                for v in client.versions(node_id)
            ]
        )

    def revert_version(node_id: str, version_id: str, comment: str = "") -> dict[str, Any]:
        return audited(
            "revert_version",
            lambda: client.revert_version(node_id, version_id, comment),
            {"node_id": node_id, "version_id": version_id, "comment": comment},
        )

    def lock_node(node_id: str) -> dict[str, Any]:
        return audited("lock_node", lambda: client.lock_node(node_id), {"node_id": node_id})

    def unlock_node(node_id: str) -> dict[str, Any]:
        return audited("unlock_node", lambda: client.unlock_node(node_id), {"node_id": node_id})

    # -------------------------------------------------------------- trashcan
    def list_deleted_nodes() -> dict[str, Any]:
        return read(lambda: [_thin(n) for n in client.deleted_nodes()])

    def restore_deleted_node(node_id: str) -> dict[str, Any]:
        return audited(
            "restore_deleted_node", lambda: client.restore_node(node_id), {"node_id": node_id}
        )

    # ------------------------------------------------- sharing and discussion
    def create_shared_link(node_id: str, expires_at: str = "") -> dict[str, Any]:
        return audited(
            "create_shared_link",
            lambda: client.create_shared_link(node_id, expires_at or None),
            {"node_id": node_id, "expires_at": expires_at},
        )

    def list_comments(node_id: str) -> dict[str, Any]:
        return read(
            lambda: [
                {"content": c.get("content"), "by": (c.get("createdBy") or {}).get("id")}
                for c in client.comments(node_id)
            ]
        )

    def add_comment(node_id: str, content: str) -> dict[str, Any]:
        return audited(
            "add_comment",
            lambda: client.add_comment(node_id, content),
            {"node_id": node_id, "content": content},
        )

    def add_favorite(node_id: str, kind: str = "file") -> dict[str, Any]:
        return audited(
            "add_favorite",
            lambda: client.add_favorite(node_id, kind),
            {"node_id": node_id, "kind": kind},
        )

    # ------------------------------------------------------- people and sites
    def create_group(group_id: str, display_name: str) -> dict[str, Any]:
        return audited(
            "create_group",
            lambda: client.create_group(group_id, display_name),
            {"group_id": group_id, "display_name": display_name},
        )

    def add_group_member(
        group_id: str, member_id: str, member_type: str = "PERSON"
    ) -> dict[str, Any]:
        return audited(
            "add_group_member",
            lambda: client.add_group_member(group_id, member_id, member_type),
            {"group_id": group_id, "member_id": member_id, "member_type": member_type},
        )

    def create_site(site_id: str, title: str, visibility: str = "PRIVATE") -> dict[str, Any]:
        return audited(
            "create_site",
            lambda: client.create_site(site_id, title, visibility),
            {"site_id": site_id, "title": title, "visibility": visibility},
        )

    def add_site_member(site_id: str, person_id: str, role: str = "SiteConsumer") -> dict[str, Any]:
        return audited(
            "add_site_member",
            lambda: client.add_site_member(site_id, person_id, role),
            {"site_id": site_id, "person_id": person_id, "role": role},
        )

    specs = [
        (
            list_actions,
            "list_actions",
            "List the repository actions available, optionally for one node. "
            "Use this FIRST when a request does not map to a named tool - it "
            "reaches capabilities such as simple workflows, counters, type "
            "specialisation, category linking, image transforms and scripts.",
        ),
        (
            describe_action,
            "describe_action",
            "The parameters a repository action takes, with which are mandatory. "
            "Call this before run_action so the parameters are correct.",
        ),
        (
            run_action,
            "run_action",
            "Execute any repository action on a node. Runs asynchronously, so "
            "verify the effect afterwards rather than assuming it is done.",
        ),
        (list_versions, "list_versions", "Version history of a document."),
        (revert_version, "revert_version", "Revert a document to an earlier version."),
        (lock_node, "lock_node", "Lock a node so others cannot edit it."),
        (unlock_node, "unlock_node", "Release a lock on a node."),
        (list_deleted_nodes, "list_deleted_nodes", "What is in the trashcan."),
        (restore_deleted_node, "restore_deleted_node", "Restore a node from the trashcan."),
        (create_shared_link, "create_shared_link", "Create a public share link for a document."),
        (list_comments, "list_comments", "Read the comments on a node."),
        (add_comment, "add_comment", "Add a comment to a node."),
        (add_favorite, "add_favorite", "Add a node to the user's favourites."),
        (create_group, "create_group", "Create a group, e.g. id 'FINANCE_REVIEWERS'."),
        (add_group_member, "add_group_member", "Add a person or group to a group."),
        (create_site, "create_site", "Create a site. Visibility PUBLIC, MODERATED or PRIVATE."),
        (add_site_member, "add_site_member", "Add a person to a site with a role."),
    ]
    return [
        StructuredTool.from_function(func=func, name=name, description=description)
        for func, name, description in specs
    ]
