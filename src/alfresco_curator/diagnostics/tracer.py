"""End-to-end pipeline tracer for Alfresco Search Enterprise.

Traces one node across the stages that actually exist in an ACS 26.2 +
Elasticsearch deployment:

    repository (source of truth)
        -> batch indexer  (polls the DB; metadata / path / content modules)
        -> elasticsearch document (per-module watermarks, extracted text, ACL)
        -> search API     (ACL filtering, then AFTS matching)

ActiveMQ is traced but is NOT on the indexing path: in this architecture
`alfresco-elasticsearch-batch-indexing` polls the repository database, so the
broker carries transform and rendition traffic only. A transform backlog is
still worth reporting, because it is what stops full text being extracted.

Every stage reports observed values; nothing here is inferred by a model.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from alfresco_curator.alfresco import afts
from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.diagnostics.telemetry import MODULE_WATERMARKS, Telemetry
from alfresco_curator.errors import AlfrescoError

log = logging.getLogger(__name__)

Status = Literal["ok", "warn", "fail"]

REPOSITORY = "Repository"
INDEXER = "Batch indexer"
BROKER = "Transform broker (ActiveMQ)"
ELASTICSEARCH = "Elasticsearch"
INDEX_ACL = "Index ACL"
SEARCH_API = "Search API / AFTS"

BROKER_NOTE = (
    "Carries transform/rendition traffic only. The search index is fed by "
    "database polling, not by JMS."
)


@dataclass(slots=True)
class Stage:
    stage: str
    status: Status
    headline: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "headline": self.headline,
            "detail": self.detail,
        }


def _age_seconds(now_ms: float, then_ms: float | None) -> float | None:
    if not then_ms:
        return None
    return round((now_ms - then_ms) / 1000.0, 1)


def _to_epoch_ms(iso_timestamp: str) -> float | None:
    try:
        return datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).timestamp() * 1000
    except (ValueError, AttributeError):
        return None


# ----------------------------------------------------------------- 1. repository


def trace_repository(client: AlfrescoClient, node_id: str) -> tuple[Stage, dict[str, Any] | None]:
    try:
        node = client.get_node_details(node_id)
    except AlfrescoError as exc:
        deleted = client.is_deleted(node_id)
        return (
            Stage(
                REPOSITORY,
                "warn" if deleted else "fail",
                "Node is in the trashcan" if deleted else f"Node unreadable: {exc.message}",
                {"error": str(exc), "httpStatus": exc.status_code, "isDeleted": deleted},
            ),
            None,
        )

    content = node.get("content") or {}
    return (
        Stage(
            REPOSITORY,
            "ok",
            f"Node exists as {node.get('name')}",
            {
                "name": node.get("name"),
                "nodeType": node.get("nodeType"),
                "modifiedAt": node.get("modifiedAt"),
                "sizeInBytes": content.get("sizeInBytes"),
                "mimeType": content.get("mimeType"),
                "isDeleted": False,
            },
        ),
        node,
    )


# -------------------------------------------------------------------- 2. indexer


def trace_indexer(telemetry: Telemetry, now_ms: float) -> tuple[Stage, float | None]:
    health = telemetry.indexer_health()
    cycle_age = _age_seconds(
        now_ms, telemetry.metric_value("alfresco.reindex.continuous.last.cycle.finished.timestamp")
    )
    activity_age = _age_seconds(
        now_ms, telemetry.metric_value("alfresco.reindex.continuous.last.activity.timestamp")
    )
    retries = telemetry.metric_value("alfresco.reindex.retries", "COUNT")
    stale_after = telemetry.poll_seconds * telemetry.stale_cycle_factor

    if health.get("error"):
        status: Status = "fail"
        headline = f"Indexer actuator unreachable ({health['error']})"
    elif health.get("status") != "UP":
        status, headline = "fail", f"Indexer reports {health.get('status')}"
    elif cycle_age is None:
        status, headline = "warn", "Indexer is up but has not completed a cycle yet"
    elif cycle_age > stale_after:
        status = "warn"
        headline = (
            f"Last indexing cycle finished {cycle_age}s ago (poll is {telemetry.poll_seconds:g}s)"
        )
    else:
        status, headline = "ok", f"Indexing cycle completed {cycle_age}s ago"

    return (
        Stage(
            INDEXER,
            status,
            headline,
            {
                "health": health.get("status") or health.get("error"),
                "lastCycleFinishedSecondsAgo": cycle_age,
                "lastActivitySecondsAgo": activity_age,
                "pollIntervalSeconds": telemetry.poll_seconds,
                "retries": retries,
                "moduleWrites": telemetry.module_write_outcomes(),
            },
        ),
        cycle_age,
    )


# --------------------------------------------------------------------- 3. broker


def trace_broker(telemetry: Telemetry) -> Stage:
    depths = telemetry.transform_queue_depths()
    backlog = sum(depth for depth in depths.values() if isinstance(depth, int))
    unreachable = all(depth is None for depth in depths.values())

    if unreachable:
        status: Status = "warn"
        headline = "Broker unreachable"
    elif backlog:
        status, headline = "warn", f"{backlog} transform message(s) queued"
    else:
        status, headline = "ok", "No transform backlog"

    return Stage(BROKER, status, headline, {"queueDepths": depths, "note": BROKER_NOTE})


# -------------------------------------------------------------- 4. elasticsearch


def trace_elasticsearch(
    telemetry: Telemetry,
    node_id: str,
    node: dict[str, Any] | None,
    now_ms: float,
) -> tuple[Stage, dict[str, Any] | None]:
    document, error = telemetry.document(node_id)
    cluster = telemetry.cluster_health()
    index_stats = telemetry.index_stats()

    if error:
        return Stage(ELASTICSEARCH, "fail", f"Index unreachable ({error})", {"error": error}), None

    if document is None:
        return (
            Stage(
                ELASTICSEARCH,
                "fail",
                "Node is NOT present in the search index",
                {
                    "index": telemetry.es_index,
                    "clusterStatus": cluster.get("status"),
                    "indexedDocuments": index_stats.get("documents"),
                },
            ),
            None,
        )

    module_ages = {
        module: _age_seconds(now_ms, document.get(field_name))
        for module, field_name in MODULE_WATERMARKS.items()
    }
    missing = [module for module, age in module_ages.items() if age is None]
    extracted_text = document.get("cm%3Acontent") or ""

    repo_modified_ms = _to_epoch_ms((node or {}).get("modifiedAt", ""))
    indexed_ms = document.get(MODULE_WATERMARKS["metadata"])
    stale = bool(repo_modified_ms and indexed_ms and repo_modified_ms > float(indexed_ms) + 1000)

    if missing:
        status: Status = "warn"
        headline = f"Indexed, but no {', '.join(missing)} data"
    elif stale:
        status, headline = "warn", "Indexed copy is older than the repository version"
    else:
        status, headline = "ok", "Indexed by all three modules"

    return (
        Stage(
            ELASTICSEARCH,
            status,
            headline,
            {
                "clusterStatus": cluster.get("status"),
                "indexedDocuments": index_stats.get("documents"),
                "moduleAgeSeconds": module_ages,
                "indexedPath": (document.get("PATH") or [None])[0],
                "indexedType": document.get("TYPE"),
                "alive": document.get("ALIVE"),
                "fullTextExtracted": bool(extracted_text.strip()),
                "extractedTextPreview": extracted_text.strip()[:160] or None,
                "staleVersusRepository": stale,
            },
        ),
        document,
    )


# ------------------------------------------------------------------- 6. index ACL


def trace_index_acl(client: AlfrescoClient, document: dict[str, Any]) -> Stage:
    readers = document.get("READER") or []
    denied = document.get("DENIED") or []
    user = client.username

    authorities = {user, "GROUP_EVERYONE"}
    try:
        authorities.update(client.get_person_groups())
    except AlfrescoError as exc:
        log.debug("could not read group memberships for %s: %s", user, exc)

    granted = sorted(a for a in authorities.intersection(readers) if a)
    blocked = sorted(a for a in authorities.intersection(denied) if a)

    return Stage(
        INDEX_ACL,
        "ok" if granted and not blocked else "fail",
        f"{user} matches READER via {', '.join(granted)}"
        if granted and not blocked
        else f"{user} is not in the indexed READER list",
        {
            "searchingAs": user,
            "readers": readers,
            "denied": denied,
            "matchedAuthorities": granted,
            "blockedBy": blocked,
        },
    )


# -------------------------------------------------------------------- 7. search


def trace_search(client: AlfrescoClient, node_id: str, afts_query: str) -> Stage:
    try:
        visible = client.count_matches(f'ID:"{afts.node_ref(node_id)}"').value > 0
        matches = client.count_matches(afts.scope_to_node(afts_query, node_id)).value > 0
    except AlfrescoError as exc:
        return Stage(SEARCH_API, "fail", f"Query rejected: {exc.message}", {"error": str(exc)})

    if matches:
        headline = "Query matches this node"
    elif visible:
        headline = "Node is searchable but the query excludes it"
    else:
        headline = "Node is not returned by the search API for this user"

    return Stage(
        SEARCH_API,
        "ok" if matches else "fail",
        headline,
        {"visibleToUser": visible, "queryMatches": matches, "query": afts_query},
    )


# ------------------------------------------------------------------- orchestration


def summarise(stages: list[Stage]) -> dict[str, Any]:
    """The first failing stage is the one worth acting on."""
    for severity in ("fail", "warn"):
        for stage in stages:
            if stage.status == severity:
                return {
                    "brokenAt": stage.stage,
                    "summary": stage.headline,
                    "status": severity,
                }
    return {
        "brokenAt": None,
        "summary": "Every stage of the pipeline is healthy.",
        "status": "ok",
    }


def trace_node(
    client: AlfrescoClient,
    node_reference: str,
    telemetry: Telemetry,
    afts_query: str | None = None,
) -> dict[str, Any]:
    """Walk one node through every stage and report what each one observed."""
    node_id = client.resolve_node_id(node_reference)
    afts.node_ref(node_id)  # rejects anything unsafe before it reaches a query
    now_ms = time.time() * 1000

    repository_stage, node = trace_repository(client, node_id)
    indexer_stage, _ = trace_indexer(telemetry, now_ms)
    elasticsearch_stage, document = trace_elasticsearch(telemetry, node_id, node, now_ms)

    stages = [repository_stage, indexer_stage, trace_broker(telemetry), elasticsearch_stage]
    if document is not None:
        stages.append(trace_index_acl(client, document))
    if afts_query and afts_query.strip():
        stages.append(trace_search(client, node_id, afts_query.strip()))

    return {
        "nodeId": node_id,
        "searchingAs": client.username,
        "stages": [stage.as_dict() for stage in stages],
        "verdict": summarise(stages),
    }
