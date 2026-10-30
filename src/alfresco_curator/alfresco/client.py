"""Alfresco REST API v1 client: ticket auth, AFTS search, node inspection."""

from __future__ import annotations

import base64
import logging
from contextlib import suppress
from typing import Any, NamedTuple

import httpx

from alfresco_curator.config import get_settings
from alfresco_curator.errors import AlfrescoError, SessionExpiredError

log = logging.getLogger(__name__)

AUTH_API = "/api/-default-/public/authentication/versions/1"
SEARCH_API = "/api/-default-/public/search/versions/1"
CORE_API = "/api/-default-/public/alfresco/versions/1"

DEFAULT_INCLUDES = ["properties", "aspectNames", "path", "permissions", "isLink"]

# The search API caps a page at 1000 rows.
MAX_PAGE = 1000


class MatchCount(NamedTuple):
    """A hit count and whether it is the real total.

    Alfresco derives `totalItems` from the page it just returned, so it is
    `skipCount + count + 1` whenever more rows exist. Asking for one row and
    reading `totalItems` reports "2" for a query matching half a million
    documents, so a count is only exact once a page has exhausted the result
    set.
    """

    value: int
    exact: bool

    def __str__(self) -> str:
        return str(self.value) if self.exact else f"{self.value}+"


def normalise_base_url(base_url: str) -> str:
    """Accept the forms people actually paste: bare host, Share URL, API root."""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise AlfrescoError(400, "ACS base URL is required")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    for suffix in ("/api/-default-/public", "/share", "/s"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    if not url.endswith("/alfresco"):
        url = f"{url}/alfresco"
    return url


class AlfrescoClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        verify: bool | None = None,
    ) -> None:
        settings = get_settings().alfresco
        self.base_url = normalise_base_url(base_url or settings.base_url)
        self.username: str | None = None
        self.ticket: str | None = None
        self.auth_source: str | None = None
        self._password: str | None = None
        self._query_language: str | None = None
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=settings.timeout_seconds if timeout is None else timeout,
            verify=settings.verify_tls if verify is None else verify,
        )

    # ------------------------------------------------------------------ auth
    def login(self, username: str, password: str) -> str:
        if not (username or "").strip() or not password:
            raise AlfrescoError(400, "username and password are required")
        resp = self._send(
            "POST",
            f"{AUTH_API}/tickets",
            json={"userId": username, "password": password},
            reauth=False,
        )
        self.ticket = resp.json()["entry"]["id"]
        self.username = username
        self._password = password
        self._apply_ticket(self.ticket)
        return self.ticket

    def login_with_ticket(self, ticket: str) -> str:
        """Adopt an existing ticket, as issued to a signed-in Share session.

        No password is held, so an expired ticket cannot be renewed here; the
        caller re-authenticates upstream instead.
        """
        ticket = (ticket or "").strip()
        if not ticket:
            raise AlfrescoError(400, "ticket is required")
        self._apply_ticket(ticket)
        self.ticket = ticket
        self._password = None
        try:
            self.username = self.get_current_user()["id"]
        except AlfrescoError:
            self._client.headers.pop("Authorization", None)
            self.ticket = None
            raise
        return self.username

    def _apply_ticket(self, ticket: str) -> None:
        # Alfresco ticket auth: Basic <base64(ticket)>, NOT the raw ticket.
        encoded = base64.b64encode(ticket.encode("utf-8")).decode("ascii")
        self._client.headers["Authorization"] = f"Basic {encoded}"

    def close(self) -> None:
        self._client.close()

    # -------------------------------------------------------------- transport
    def _raw(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise AlfrescoError(408, f"timed out talking to {self.base_url}") from exc
        except httpx.RequestError as exc:
            raise AlfrescoError(0, f"cannot reach {self.base_url}: {exc}") from exc

    def _send(self, method: str, url: str, *, reauth: bool = True, **kwargs: Any) -> httpx.Response:
        resp = self._raw(method, url, **kwargs)
        # Tickets expire server-side; re-authenticate once and replay.
        if resp.status_code == 401 and reauth:
            if self.username and self._password:
                self.login(self.username, self._password)
                resp = self._raw(method, url, **kwargs)
            elif self.ticket:
                # Borrowed ticket, no password: only the host can renew it.
                raise SessionExpiredError(401, "Alfresco session expired")
        self._check(resp)
        return resp

    # ---------------------------------------------------------------- search
    def search_afts(
        self,
        afts_query: str,
        max_items: int = 25,
        skip_count: int = 0,
        includes: list[str] | None = None,
        fields: list[str] | None = None,
        language: str = "afts",
    ) -> dict[str, Any]:
        query = (afts_query or "").strip()
        if not query:
            raise AlfrescoError(400, f"{language} query is empty")
        payload: dict[str, Any] = {
            "query": {"query": query, "language": language},
            "paging": {
                "maxItems": max(1, min(max_items, MAX_PAGE)),
                "skipCount": max(0, skip_count),
            },
            "include": DEFAULT_INCLUDES if includes is None else includes,
        }
        if fields:
            payload["fields"] = fields
        return self._send("POST", f"{SEARCH_API}/search", json=payload).json()

    def count_matches(self, afts_query: str, language: str = "afts") -> MatchCount:
        """Hit count for a query, and whether the repository could confirm it.

        Rows are requested a full page at a time and reduced to ids, because
        the count is only trustworthy once the page has drained the results.
        """
        result = self.search_afts(
            afts_query, max_items=MAX_PAGE, includes=[], fields=["id"], language=language
        )
        pagination = result.get("list", {}).get("pagination", {})
        has_more = bool(pagination.get("hasMoreItems"))
        total = int(pagination.get("totalItems", 0))
        return MatchCount(total - 1 if has_more else total, not has_more)

    def detect_query_language(self) -> str:
        """Which query language this repository actually accepts.

        Search Enterprise answers AFTS; older Solr-backed deployments may only
        answer Lucene. Asking beats guessing, and the answer cannot change
        under a running repository, so it is resolved once per client.
        """
        if self._query_language is not None:
            return self._query_language

        configured = get_settings().alfresco.query_language
        if configured != "auto":
            self._query_language = configured
            return configured

        for candidate in ("afts", "lucene"):
            try:
                self.search_afts(
                    'TYPE:"cm:content"',
                    max_items=1,
                    includes=[],
                    fields=["id"],
                    language=candidate,
                )
            except AlfrescoError:
                continue
            self._query_language = candidate
            return candidate

        # Nothing answered: keep the supported default rather than fail the
        # request, so the caller sees a search error instead of a startup one.
        self._query_language = "afts"
        return self._query_language

    # ----------------------------------------------------------------- nodes
    def get_node_details(self, node_id: str) -> dict[str, Any]:
        resp = self._send(
            "GET",
            f"{CORE_API}/nodes/{node_id}",
            params={"include": ",".join(DEFAULT_INCLUDES)},
        )
        return resp.json()["entry"]

    def resolve_node_id(self, reference: str) -> str:
        """Accept a bare UUID, a NodeRef, or a repository path."""
        reference = (reference or "").strip()
        if not reference:
            raise AlfrescoError(400, "node reference is empty")
        if "://" in reference:
            return reference.rsplit("/", 1)[-1]
        if reference.startswith("/"):
            relative = reference.strip("/")
            # Share and the REST path field both display paths rooted at
            # Company Home, which is -root- for relativePath lookups.
            for prefix in ("Company Home/", "company_home/"):
                if relative.startswith(prefix):
                    relative = relative[len(prefix) :]
            resp = self._send("GET", f"{CORE_API}/nodes/-root-", params={"relativePath": relative})
            return resp.json()["entry"]["id"]
        return reference

    def find_folders(self, name: str, max_items: int = 100) -> list[dict[str, Any]]:
        """Find exact-name folders visible to this user's repository ticket."""
        name = (name or "").strip()
        if not name or len(name) > 255 or any(char in name for char in '"\\'):
            raise AlfrescoError(
                400, "folder name must be exact and contain no quotes or backslashes"
            )
        language = self.detect_query_language()
        clause = f'=cm:name:"{name}"' if language == "afts" else f'cm:name:"{name}"'
        payload = self.search_afts(
            f'TYPE:"cm:folder" AND {clause}', max_items=max_items, language=language
        )
        entries = payload.get("list", {}).get("entries", [])
        return [
            {
                "id": entry.get("id"),
                "name": entry.get("name"),
                "path": (entry.get("path") or {}).get("name"),
            }
            for item in entries
            if (entry := item.get("entry", {})).get("name") == name
            and entry.get("nodeType") == "cm:folder"
        ]

    def is_deleted(self, node_id: str) -> bool:
        """True when the node is in the trashcan, which removes it from the index."""
        try:
            self._send("GET", f"{CORE_API}/deleted-nodes/{node_id}")
        except AlfrescoError:
            return False
        return True

    # ----------------------------------------------------------------- writes
    # Every call below runs as the signed-in user, so the repository applies
    # that user's permissions rather than anything the agent believes.

    def create_folder(
        self,
        parent_id: str,
        name: str,
        title: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "nodeType": "cm:folder"}
        properties = {}
        if title:
            properties["cm:title"] = title
        if description:
            properties["cm:description"] = description
        if properties:
            body["properties"] = properties
        resp = self._send("POST", f"{CORE_API}/nodes/{parent_id}/children", json=body)
        return resp.json()["entry"]

    def update_node(
        self,
        node_id: str,
        *,
        name: str | None = None,
        properties: dict[str, Any] | None = None,
        aspects: list[str] | None = None,
    ) -> dict[str, Any]:
        """Rename, set properties, or add aspects in a single request."""
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if properties:
            body["properties"] = properties
        if aspects is not None:
            body["aspectNames"] = aspects
        if not body:
            raise AlfrescoError(400, "nothing to update")
        return self._send("PUT", f"{CORE_API}/nodes/{node_id}", json=body).json()["entry"]

    def add_aspects(self, node_id: str, aspects: list[str]) -> dict[str, Any]:
        """Aspects are replaced wholesale by the API, so merge before writing."""
        current = self.get_node_details(node_id).get("aspectNames") or []
        merged = list(dict.fromkeys([*current, *aspects]))
        return self.update_node(node_id, aspects=merged)

    def move_node(
        self, node_id: str, target_parent_id: str, name: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"targetParentId": target_parent_id}
        if name:
            body["name"] = name
        return self._send("POST", f"{CORE_API}/nodes/{node_id}/move", json=body).json()["entry"]

    def copy_node(
        self, node_id: str, target_parent_id: str, name: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"targetParentId": target_parent_id}
        if name:
            body["name"] = name
        return self._send("POST", f"{CORE_API}/nodes/{node_id}/copy", json=body).json()["entry"]

    def list_children(self, node_id: str, max_items: int = 100) -> list[dict[str, Any]]:
        resp = self._send(
            "GET",
            f"{CORE_API}/nodes/{node_id}/children",
            params={"maxItems": max_items, "include": "properties,aspectNames,path"},
        )
        return [item["entry"] for item in resp.json()["list"]["entries"]]

    def add_tag(self, node_id: str, tag: str) -> dict[str, Any]:
        return self._send("POST", f"{CORE_API}/nodes/{node_id}/tags", json={"tag": tag}).json()[
            "entry"
        ]

    def remove_tag(self, node_id: str, tag: str) -> None:
        """Tags are removed by id, so the node's own list has to be searched."""
        resp = self._send("GET", f"{CORE_API}/nodes/{node_id}/tags")
        for item in resp.json()["list"]["entries"]:
            if item["entry"].get("tag") == tag:
                self._send("DELETE", f"{CORE_API}/nodes/{node_id}/tags/{item['entry']['id']}")
                return
        raise AlfrescoError(404, f"tag {tag!r} is not on this node")

    def set_permission(
        self, node_id: str, authority: str, role: str, *, inherit: bool = True
    ) -> dict[str, Any]:
        """Grant one authority a role, preserving the entries already set."""
        current = self.get_node_details(node_id).get("permissions") or {}
        entries = [
            {
                "authorityId": entry["authorityId"],
                "name": entry["name"],
                "accessStatus": entry.get("accessStatus", "ALLOWED"),
            }
            for entry in current.get("locallySet") or []
            if entry.get("authorityId") != authority
        ]
        entries.append({"authorityId": authority, "name": role, "accessStatus": "ALLOWED"})
        body = {"permissions": {"isInheritanceEnabled": inherit, "locallySet": entries}}
        return self._send("PUT", f"{CORE_API}/nodes/{node_id}", json=body).json()["entry"]

    # ------------------------------------------------------- repository actions
    # The repository publishes what it can do. Reading that catalogue at run
    # time is what lets the agent reach features nobody remembers exist.

    def action_definitions(self, max_items: int = 200) -> list[dict[str, Any]]:
        resp = self._send("GET", f"{CORE_API}/action-definitions", params={"maxItems": max_items})
        return [item["entry"] for item in resp.json()["list"]["entries"]]

    def action_definition(self, action_id: str) -> dict[str, Any]:
        return self._send("GET", f"{CORE_API}/action-definitions/{action_id}").json()["entry"]

    def actions_for_node(self, node_id: str, max_items: int = 200) -> list[dict[str, Any]]:
        """Only the actions the repository considers valid for this node."""
        resp = self._send(
            "GET",
            f"{CORE_API}/nodes/{node_id}/action-definitions",
            params={"maxItems": max_items},
        )
        return [item["entry"] for item in resp.json()["list"]["entries"]]

    def execute_action(
        self, action_id: str, target_id: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Queue a repository action. Execution is asynchronous."""
        body: dict[str, Any] = {"actionDefinitionId": action_id, "targetId": target_id}
        if params:
            body["params"] = params
        return self._send("POST", f"{CORE_API}/action-executions", json=body).json()["entry"]

    def folder_rules(self, folder_id: str, max_items: int = 100) -> list[dict[str, Any]]:
        """List rules attached to a folder's default rule set."""
        try:
            response = self._send(
                "GET",
                f"{CORE_API}/nodes/{folder_id}/rule-sets/-default-/rules",
                params={"maxItems": max_items},
            ).json()
        except AlfrescoError as exc:
            if exc.status_code == 404:
                return []
            raise
        return [item["entry"] for item in response.get("list", {}).get("entries", [])]

    def create_folder_rule(self, folder_id: str, rule: dict[str, Any]) -> dict[str, Any]:
        """Create an ACS document-management rule on a folder."""
        response = self._send(
            "POST",
            f"{CORE_API}/nodes/{folder_id}/rule-sets/-default-/rules",
            json=rule,
        ).json()
        return response.get("entry", response)

    # --------------------------------------------------------------- versions
    def versions(self, node_id: str) -> list[dict[str, Any]]:
        resp = self._send("GET", f"{CORE_API}/nodes/{node_id}/versions")
        return [item["entry"] for item in resp.json()["list"]["entries"]]

    def revert_version(self, node_id: str, version_id: str, comment: str = "") -> dict[str, Any]:
        body = {"comment": comment, "majorVersion": True}
        return self._send(
            "POST", f"{CORE_API}/nodes/{node_id}/versions/{version_id}/revert", json=body
        ).json()["entry"]

    def lock_node(self, node_id: str, *, lock_type: str = "ALLOW_OWNER_CHANGES") -> dict[str, Any]:
        return self._send(
            "POST", f"{CORE_API}/nodes/{node_id}/lock", json={"type": lock_type}
        ).json()["entry"]

    def unlock_node(self, node_id: str) -> dict[str, Any]:
        return self._send("POST", f"{CORE_API}/nodes/{node_id}/unlock").json()["entry"]

    # -------------------------------------------------------------- trashcan
    def deleted_nodes(self, max_items: int = 50) -> list[dict[str, Any]]:
        resp = self._send("GET", f"{CORE_API}/deleted-nodes", params={"maxItems": max_items})
        return [item["entry"] for item in resp.json()["list"]["entries"]]

    def restore_node(self, node_id: str) -> dict[str, Any]:
        return self._send("POST", f"{CORE_API}/deleted-nodes/{node_id}/restore").json()["entry"]

    # ------------------------------------------------- sharing and discussion
    def create_shared_link(self, node_id: str, expires_at: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"nodeId": node_id}
        if expires_at:
            body["expiresAt"] = expires_at
        return self._send("POST", f"{CORE_API}/shared-links", json=body).json()["entry"]

    def comments(self, node_id: str) -> list[dict[str, Any]]:
        resp = self._send("GET", f"{CORE_API}/nodes/{node_id}/comments")
        return [item["entry"] for item in resp.json()["list"]["entries"]]

    def add_comment(self, node_id: str, content: str) -> dict[str, Any]:
        return self._send(
            "POST", f"{CORE_API}/nodes/{node_id}/comments", json={"content": content}
        ).json()["entry"]

    def add_favorite(self, node_id: str, kind: str = "file") -> dict[str, Any]:
        body = {"target": {kind: {"guid": node_id}}}
        return self._send("POST", f"{CORE_API}/people/-me-/favorites", json=body).json()["entry"]

    # -------------------------------------------------------- people and sites
    def create_group(self, group_id: str, display_name: str) -> dict[str, Any]:
        body = {"id": group_id, "displayName": display_name}
        return self._send("POST", f"{CORE_API}/groups", json=body).json()["entry"]

    def add_group_member(
        self, group_id: str, member_id: str, member_type: str = "PERSON"
    ) -> dict[str, Any]:
        body = {"id": member_id, "memberType": member_type}
        return self._send("POST", f"{CORE_API}/groups/{group_id}/members", json=body).json()[
            "entry"
        ]

    def create_site(self, site_id: str, title: str, visibility: str = "PRIVATE") -> dict[str, Any]:
        body = {"id": site_id, "title": title, "visibility": visibility}
        return self._send("POST", f"{CORE_API}/sites", json=body).json()["entry"]

    def add_site_member(
        self, site_id: str, person_id: str, role: str = "SiteConsumer"
    ) -> dict[str, Any]:
        body = {"id": person_id, "role": role}
        return self._send("POST", f"{CORE_API}/sites/{site_id}/members", json=body).json()["entry"]

    def delete_node(self, node_id: str, *, permanent: bool = False) -> None:
        """Delete a node and its children; `permanent` bypasses the trashcan."""
        self._send(
            "DELETE",
            f"{CORE_API}/nodes/{node_id}",
            params={"permanent": "true"} if permanent else None,
        )

    def get_current_user(self) -> dict[str, Any]:
        return self._send("GET", f"{CORE_API}/people/-me-").json()["entry"]

    def get_rendition(self, node_id: str, rendition: str = "doclib") -> tuple[bytes, str]:
        """Fetch a rendition's bytes, for proxying a thumbnail to the browser.

        The browser has no Alfresco credentials of its own, so the image cannot
        be linked directly. `placeholder=true` makes the repository return a
        mimetype icon rather than 404 while a rendition is still being made.
        """
        resp = self._send(
            "GET",
            f"{CORE_API}/nodes/{node_id}/renditions/{rendition}/content",
            params={"attachment": "false", "placeholder": "true"},
        )
        return resp.content, resp.headers.get("content-type", "image/png")

    def rendition_status(self, node_id: str, rendition: str = "doclib") -> str:
        resp = self._send("GET", f"{CORE_API}/nodes/{node_id}/renditions/{rendition}")
        return str(resp.json()["entry"].get("status", "NOT_CREATED"))

    def request_rendition(self, node_id: str, rendition: str = "doclib") -> None:
        """Ask for a rendition to be generated; it appears on a later request."""
        # Already queued, or the type has no such rendition.
        with suppress(AlfrescoError):
            self._send("POST", f"{CORE_API}/nodes/{node_id}/renditions", json={"id": rendition})

    def get_person_groups(self, username: str | None = None) -> list[str]:
        """Authority ids the user belongs to, used to interpret indexed ACLs."""
        person = username or self.username
        if not person:
            return []
        resp = self._send("GET", f"{CORE_API}/people/{person}/groups", params={"maxItems": 100})
        entries = resp.json().get("list", {}).get("entries", [])
        return [entry["entry"]["id"] for entry in entries]

    # ----------------------------------------------------------------- misc
    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.is_success:
            return
        detail = resp.text
        try:
            error = resp.json()["error"]
            detail = error.get("briefSummary") or error.get("errorKey") or detail
        except Exception:
            pass
        raise AlfrescoError(resp.status_code, detail[:500])
