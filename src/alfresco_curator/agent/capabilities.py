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
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from alfresco_curator import governance
from alfresco_curator.agent.actions import AuditLog, _thin
from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.errors import AlfrescoError, explain_alfresco_error, is_repository_read_only

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
    "create_folder_rule",
)


class FolderRuleCondition(BaseModel):
    field: str = Field(min_length=1, max_length=255)
    comparator: str = Field(min_length=1, max_length=64)
    parameter: str = Field(min_length=1, max_length=1000)


def build_capability_tools(
    client: AlfrescoClient,
    audit: AuditLog,
    *,
    allow_writes: bool = True,
) -> list[StructuredTool]:
    def audited(name: str, call, arguments: dict[str, Any]) -> dict[str, Any]:
        entry = audit.record(name, arguments, client.username or "unknown")
        if audit.write_blocked_reason:
            entry.outcome, entry.detail = "failed", audit.write_blocked_reason
            governance.record_decision(
                name, arguments, entry.user, entry.outcome, reversible=False, detail=entry.detail
            )
            return {"ok": False, "error": audit.write_blocked_reason}
        denied = governance.change_denial(name)
        if denied:
            entry.outcome, entry.detail = "failed", denied
            governance.record_decision(
                name, arguments, entry.user, entry.outcome, reversible=False, detail=denied
            )
            return {"ok": False, "error": denied}
        audit.budget.limit = governance.effective_policy()["mutation_budget"]
        over = audit.budget.take(name)
        if over:
            entry.outcome, entry.detail = "failed", over
            governance.record_decision(
                name, arguments, entry.user, entry.outcome, reversible=False, detail=entry.detail
            )
            return {"ok": False, "error": over}
        try:
            result = call()
        except AlfrescoError as exc:
            entry.outcome, entry.detail = "failed", explain_alfresco_error(exc)
            if is_repository_read_only(exc):
                audit.write_blocked_reason = entry.detail
            governance.record_decision(
                name, arguments, entry.user, entry.outcome, reversible=False, detail=entry.detail
            )
            return {"ok": False, "error": entry.detail}
        entry.outcome = "ok"
        entry.detail = result.get("id", "") if isinstance(result, dict) else ""
        entry.created_id = entry.detail
        governance.record_decision(
            name, arguments, entry.user, entry.outcome, reversible=False, detail=entry.detail
        )
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

    def find_folders(name: str) -> dict[str, Any]:
        """Resolve an exact human folder name to accessible node IDs and paths."""
        return read(lambda: {"matches": client.find_folders(name)})

    def resolve_folder(reference: str) -> tuple[str, dict[str, Any]]:
        try:
            folder_id = client.resolve_node_id(reference)
            folder = client.get_node_details(folder_id)
        except AlfrescoError as exc:
            if exc.status_code not in (400, 404):
                raise
            matches = client.find_folders(reference)
            if len(matches) > 1:
                paths = ", ".join(match.get("path") or match["id"] for match in matches[:10])
                raise AlfrescoError(
                    409,
                    f"Several accessible folders are named {reference!r}: {paths}. "
                    "Ask which path the user means.",
                ) from exc
            if not matches:
                raise AlfrescoError(
                    404,
                    f"No accessible folder named {reference!r} was found. Ask for its full path.",
                ) from exc
            folder_id = matches[0]["id"]
            folder = client.get_node_details(folder_id)
        if folder.get("nodeType") != "cm:folder" and folder.get("isFolder") is not True:
            raise AlfrescoError(400, f"{reference!r} is not a folder.")
        return folder_id, folder

    def list_folder_rules(folder: str) -> dict[str, Any]:
        """List rules attached to a folder, using this user's repository access."""
        try:
            folder_id, details = resolve_folder(folder)
            return {
                "ok": True,
                "result": {
                    "folder": {
                        "id": folder_id,
                        "name": details.get("name"),
                        "path": details.get("path"),
                    },
                    "rules": client.folder_rules(folder_id),
                },
            }
        except AlfrescoError as exc:
            return {"ok": False, "error": str(exc)}

    def create_folder_rule(
        folder: str,
        name: str,
        action_id: str,
        trigger: Literal["inbound", "update", "outbound"] = "inbound",
        description: str = "",
        inheritable: bool = False,
        params: dict[str, Any] | None = None,
        conditions: list[FolderRuleCondition] | None = None,
        boolean_mode: Literal["and", "or"] = "and",
        apply_to_all: bool = False,
    ) -> dict[str, Any]:
        """Create an Alfresco document-management rule attached to a folder."""
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 200:
            return {"ok": False, "error": "Rule name must be between 1 and 200 characters."}
        condition_values = [condition.model_dump() for condition in conditions or []]
        if not condition_values and not apply_to_all:
            return {
                "ok": False,
                "error": "Add at least one condition, or explicitly set apply_to_all=true "
                "for every item in the folder.",
            }
        if condition_values and apply_to_all:
            return {"ok": False, "error": "Use conditions or apply_to_all, not both."}

        arguments = {
            "folder": folder,
            "name": clean_name,
            "action_id": action_id,
            "trigger": trigger,
            "description": description,
            "inheritable": inheritable,
            "params": params or {},
            "conditions": condition_values,
            "boolean_mode": boolean_mode,
            "apply_to_all": apply_to_all,
        }

        def create() -> dict[str, Any]:
            folder_id, _ = resolve_folder(folder)
            available_actions = {action["id"] for action in client.actions_for_node(folder_id)}
            if action_id not in available_actions:
                raise AlfrescoError(
                    400,
                    f"Action {action_id!r} is not available for folder {folder!r}.",
                )
            definition = client.action_definition(action_id)
            if not definition:
                raise AlfrescoError(404, f"Alfresco action {action_id!r} was not found.")
            rule: dict[str, Any] = {
                "name": clean_name,
                "description": description,
                "isEnabled": True,
                "isInheritable": inheritable,
                "triggers": [trigger],
                "actions": [{"actionDefinitionId": action_id, "params": params or {}}],
            }
            if condition_values:
                rule["conditions"] = {
                    "booleanMode": boolean_mode,
                    "inverted": False,
                    "compositeConditions": [],
                    "simpleConditions": condition_values,
                }
            return client.create_folder_rule(folder_id, rule)

        return audited("create_folder_rule", create, arguments)

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
            find_folders,
            "find_folders",
            "Resolve an exact folder name to node IDs and paths visible to the signed-in user. "
            "Call this before proposing create_folder when the user names a parent folder. "
            "If multiple matches are returned, ask which path they mean; never guess.",
        ),
        (
            list_folder_rules,
            "list_folder_rules",
            "List the document-management rules attached to an ACL-visible Alfresco folder.",
        ),
        (
            create_folder_rule,
            "create_folder_rule",
            "Create an Alfresco document-management rule attached to a folder. "
            "Resolve the folder, then use list_actions and describe_action to verify the action "
            "and parameters. Rules affect future items, not existing content. "
            "Ask for conditions unless the user explicitly wants every item, then set "
            "apply_to_all=true. "
            "Always present the full rule for human approval.",
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
        if name not in MUTATING_CAPABILITIES
        or (allow_writes and not governance.change_denial(name))
    ]
