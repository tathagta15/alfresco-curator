"""The deterministic probes, exposed as LangChain tools.

These are the same functions the graph calls directly. Wrapping them makes
them composable - usable from a LangChain agent, traced individually in
LangSmith, or bound to a model that is allowed to choose.

The graph does NOT drive itself through these. A diagnosis has to be
reproducible, so the escalation order stays in code; what a tool gives you is
the option to ask a single question on its own.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import StructuredTool

from alfresco_curator import governance
from alfresco_curator.agent.guardrails import guard_result
from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.config import get_settings
from alfresco_curator.diagnostics.search_debugger import (
    analyse_conflict,
    analyse_empty_result,
    analyse_path_reach,
    collect_facts,
)
from alfresco_curator.diagnostics.telemetry import Telemetry
from alfresco_curator.diagnostics.tracer import trace_node
from alfresco_curator.errors import AlfrescoError

log = logging.getLogger(__name__)


def build_tools(client: AlfrescoClient, telemetry: Telemetry | None = None) -> list[StructuredTool]:
    """Bind the probes to one authenticated session.

    Permissions are part of the answer here, so a tool is only meaningful
    against a specific user's client - hence a factory rather than a module
    level list.
    """
    language = client.detect_query_language()

    def safely(call):
        """A malformed query is the model's problem to fix, not a crash.

        The repository answers an invalid query with a 500. Raising that would
        abandon the whole run; returned, it becomes feedback the agent can act
        on - and it does retry with a corrected query.

        Results pass through the guardrail on the way out: names, titles and
        descriptions are written by whoever owns the document, not by the
        person asking, so anything shaped like an instruction is fenced here
        rather than reaching the model looking like one.
        """
        try:
            return guard_result({"ok": True, **call()})
        except AlfrescoError as exc:
            log.info("tool call rejected by the repository: %s", exc)
            return {"ok": False, "error": str(exc)}

    def search(query: str, max_items: int = 10) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            payload = client.search_afts(query, max_items=max_items, language=language)
            listing = payload.get("list", {})
            results = [
                {
                    "id": item["entry"].get("id"),
                    "name": item["entry"].get("name"),
                    "path": (item["entry"].get("path") or {}).get("name"),
                }
                for item in listing.get("entries", [])
            ]
            # The agent hands tool results straight to the model, so this is
            # the point the egress can still be named field by field.
            governance.record_egress(
                "agent_search",
                client.username or "unknown",
                [row["id"] for row in results],
                {"id", "name", "path"},
            )
            return {
                "total": listing.get("pagination", {}).get("totalItems", 0),
                "results": results,
            }

        return safely(run)

    def clause_reach(query: str) -> dict[str, Any]:
        return safely(lambda: {"clauses": analyse_empty_result(client, query, language=language)})

    def path_reach(query: str) -> dict[str, Any]:
        return safely(lambda: {"path": analyse_path_reach(client, query, language=language)})

    def narrow_conflict(query: str) -> dict[str, Any]:
        return safely(lambda: {"narrowing": analyse_conflict(client, query, language=language)})

    def explain_node(query: str, node_id: str) -> dict[str, Any]:
        return safely(lambda: {"facts": collect_facts(client, query, node_id, language=language)})

    def trace_pipeline(node_id: str) -> dict[str, Any]:
        if telemetry is None:
            return {"ok": False, "error": "telemetry is not configured"}
        return safely(lambda: {"pipeline": trace_node(client, node_id, telemetry, "")})

    def recommend_embeddings() -> dict[str, Any]:
        """Explain local suggestion embeddings and the separate search boundary."""
        semantic = get_settings().semantic
        return {
            "current": {
                "search": "Alfresco AFTS plus concept expansion after lexical zero hits",
                "vectorSearchEnabled": False,
                "suggestionEmbeddingsEnabled": semantic.enabled,
                "suggestionEmbeddingModel": semantic.model_name if semantic.enabled else None,
                "embeddedFields": ["name", "path"],
                "reason": (
                    "Local CPU embeddings rank suggestion chips only; document retrieval remains "
                    "AFTS under the signed-in user's ticket."
                ),
            },
            "recommended": {
                "placement": (
                    "For full vector retrieval, use an on-prem or approved EEA-hosted index"
                ),
                "retrieval": [
                    "Keep AFTS for exact metadata filters and repository-native search.",
                    "Use vectors only to improve candidate recall for concept-based requests.",
                    "Store Alfresco node IDs and version stamps with each vector; do not copy ACLs "
                    "as the authorization source.",
                    "Re-fetch every candidate through this signed-in user's Alfresco ticket "
                    "before showing it.",
                ],
                "lifecycle": [
                    "Synchronize updates, versions, moves and deletes; remove stale vectors.",
                    "Keep embeddings and source text inside the approved security boundary.",
                    "Evaluate recall and false positives using permission-filtered queries "
                    "and deletion tests.",
                ],
            },
            "securityBoundary": "Alfresco remains the source of truth and ACL enforcement point.",
        }

    specs = [
        (search, "search_repository", "Run a repository query and return the hits."),
        (
            clause_reach,
            "clause_reach",
            "Hits for each top-level clause on its own. Exposes the clause that "
            "matches nothing and so empties the whole query.",
        ),
        (
            path_reach,
            "path_reach",
            "Walk a PATH clause back up its ancestors to find the folder step "
            "that stops resolving.",
        ),
        (
            narrow_conflict,
            "narrow_conflict",
            "Re-add clauses one at a time to find which combination empties a "
            "result whose clauses all match individually.",
        ),
        (
            explain_node,
            "explain_node",
            "Replay a query clause by clause against one node, to see which clause excludes it.",
        ),
        (
            trace_pipeline,
            "trace_pipeline",
            "Trace repository, indexer, broker and Elasticsearch for one node. "
            "Costs several round trips, so use it only once the query is cleared.",
        ),
        (
            recommend_embeddings,
            "recommend_embeddings",
            "Explain current search capabilities and recommend an ACL-preserving embedding "
            "architecture. "
            "Always distinguish configured behavior from future recommendations.",
        ),
    ]
    return [
        StructuredTool.from_function(func=func, name=name, description=description)
        for func, name, description in specs
    ]
