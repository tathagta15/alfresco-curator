"""Root-cause engine: why did node X not match AFTS query Q?

The LLM is only asked to explain, never to guess. Ground truth is collected
first by replaying the query clause-by-clause scoped to the target node, so the
report states which specific predicate eliminated it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from alfresco_curator import llm
from alfresco_curator.alfresco import afts, lucene
from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.diagnostics.telemetry import Telemetry
from alfresco_curator.diagnostics.tracer import trace_node
from alfresco_curator.errors import AlfrescoError

log = logging.getLogger(__name__)

DIAGNOSTIC_SYSTEM_PROMPT = """You are an Alfresco Search diagnostic engine
(Search Enterprise / Elasticsearch and Search Services / Solr).

You are given:
- an AFTS query,
- the identity of the searching user,
- DETERMINISTIC PROBE RESULTS: the query replayed clause-by-clause, scoped to
  the single target node, executed as that user,
- the node's live metadata, aspects, path and permissions (read via the
  repository REST API),
- optionally PIPELINE EVIDENCE: live telemetry from each stage of the indexing
  pipeline, collected only when the index rather than the query is suspect.

The probe results and pipeline evidence are authoritative facts. Do not
contradict them and do not speculate about clauses the probes show as matching.

Interpretation guide:
- query_rejected_by_repository = true means the AFTS is INVALID: every probe
  errored. Report the syntax fault and stop; do not blame the document.
- query_syntax_problems lists structural faults found before execution. Quote
  them if present.
- node_is_deleted = true means the node is in the trashcan, so it is correctly
  absent from the index. That is the whole answer; permissions are irrelevant.
- clauses_truncated = true means only the first clauses were probed, so an
  unprobed clause may also fail. Say so rather than claiming completeness.
- node_visible_to_user = false means the node is either not indexed yet or the
  user's ACL denies Read. Compare against the permissions payload to tell those
  two apart, and use the PIPELINE EVIDENCE below when it is present.
- node_readable_by_searching_user = false (an HTTP 403 on the node read) is
  hard proof of an ACL denial, not an indexing problem. When the metadata below
  was captured with elevated credentials, say so and quote the ACL.
- If the metadata is unavailable (403 with no elevated inspection) you do NOT
  know where the node lives. Never infer its path or parent folder from the
  PATH clause of the query: that clause is where the user searched, not where
  the node is. Say the location cannot be determined and recommend enabling
  elevated inspection.
- A document almost always reports isInheritanceEnabled = true, because the
  restriction usually lives on an ANCESTOR FOLDER. Never conclude "inheritance
  is enabled, so permissions are fine". Read the node's permissions.inherited
  list and the PARENT FOLDER ACL block: if the only granted authorities are
  administrative groups, name the parent folder as the blocker.
- A failing clause on a property that IS present in the metadata with an
  apparently matching value usually means tokenisation: hyphens/underscores
  split terms, '=' forces untokenised match, leading wildcards need cm:name
  rather than TEXT, d:text vs d:mltext analysers differ, and untokenised
  properties are case sensitive.
- `tokenisation` carries the ANSWER to that, measured against the live index
  rather than guessed: queryTokens is how Elasticsearch split the value in the
  clause, storedTokens is how it split the value actually held on the node.
  When splitsIntoSeveralTerms is true, quote both token lists and say the
  phrase is matched as separate terms. When tokensPresentInStored is true but
  the clause still failed, the terms exist but not as that phrase - recommend
  wildcards or '=' rather than changing the document.
- `path_containment` settles the location question outright:
  nodeIsInsideQueryPath = false means the document is simply somewhere else -
  quote nodePath against queryPath. Do not describe this as a folder-name
  typo when the node has a real path.
- `node_acl` exposes isInheritanceEnabled and locallySet directly. Inheritance
  being enabled is the normal case and proves nothing by itself.
- Date clauses fail on timezone/boundary errors; compare the ISO values.
- TYPE fails when the node is a subtype and EXACTTYPE was used, or vice versa.
- PATH fails when '//*' vs '/*' depth is wrong or the QName path differs from
  the display path.

Reading PIPELINE EVIDENCE (infrastructure, not query, faults):
- Each stage reports an observed status of ok / warn / fail, and
  verdict.brokenAt names the first stage worth acting on. Quote the observed
  numbers - lag seconds, watermarks, queue depths - never adjectives alone.
- The repository holding the node while Elasticsearch does not have the
  document is an INDEXING failure, not a query fault. Say so plainly, name the
  indexer stage, and cite its lag and last completed cycle.
- Indexing in this architecture is DATABASE POLLING by the batch indexer.
  ActiveMQ is NOT on the indexing path. Never write that an index event is
  "stuck in a JMS queue". A broker backlog delays transforms and renditions,
  which is what starves full-text extraction.
- If the Elasticsearch document exists but its content watermark trails the
  metadata watermark, the text has not been extracted yet: TEXT: and
  cm:content clauses fail while metadata clauses pass. Attribute that to the
  transform stage, not to the document's metadata.
- If the index holds a stale ACL, a permission change may not have propagated;
  distinguish that from the repository ACL, which the node payload shows.
- If every pipeline stage is healthy, say so and return to the clause
  evidence; do not invent an infrastructure fault.
- A hit count is a LOWER BOUND whenever more rows exist beyond the page the
  repository returned. Never present such a number as a total, and never
  conclude a document is missing because a count looks small.

Return Markdown with exactly these sections:
### Root Cause
One sentence naming the exact cause - a specific failing clause, or the
specific pipeline stage that broke.
### Evidence
Bullets citing concrete probe results, field values and, when present, the
observed pipeline numbers (quote real values).
### Recommended Fix
Two subsections: **Fix the query** (a corrected AFTS query in a code block that
would match this node while keeping the user's original intent) and **Fix the
content or platform** (repository-side change: add aspect, set property, grant
permission, or the operational action for the failing pipeline stage such as
reindexing a range, clearing a transform backlog or restarting the indexer).
"""


# Each clause costs one search request; a pathological query should not turn
# a diagnosis into dozens of round trips.
MAX_CLAUSE_PROBES = 12


def collect_facts(
    client: AlfrescoClient, afts_query: str, node_id: str, language: str = "afts"
) -> dict[str, Any]:
    """Replay the query against the single target node, clause by clause."""
    facts: dict[str, Any] = {
        "node_id": node_id,
        "node_ref": afts.node_ref(node_id),
        "searching_user": client.username,
        "query_language": language,
        # Only AFTS has a structural checker; Lucene is left to the repository.
        "query_syntax_problems": afts.syntax_problems(afts_query) if language == "afts" else [],
        "whole_query_matches_node": None,
        "node_visible_to_user": None,
        "node_is_deleted": None,
        "clause_isolation_supported": _is_pure_conjunction(afts_query, language),
        "clauses": [],
        "clauses_truncated": False,
        "probe_errors": [],
    }

    def probe(query: str) -> int | None:
        try:
            return client.count_matches(query, language=language).value
        except AlfrescoError as exc:
            facts["probe_errors"].append({"query": query, "error": str(exc)})
            return None

    id_only = f'ID:"{facts["node_ref"]}"'
    visible = probe(id_only)
    facts["node_visible_to_user"] = None if visible is None else visible > 0

    # An ID probe that finds nothing is ambiguous until the trashcan is ruled out.
    if facts["node_visible_to_user"] is False:
        facts["node_is_deleted"] = client.is_deleted(node_id)

    whole = probe(_scope(afts_query, node_id, language))
    facts["whole_query_matches_node"] = None if whole is None else whole > 0

    if facts["node_visible_to_user"]:
        clauses = _split_conjunction(afts_query, language)
        if len(clauses) > MAX_CLAUSE_PROBES:
            facts["clauses_truncated"] = True
            clauses = clauses[:MAX_CLAUSE_PROBES]
        for clause in clauses:
            hits = probe(_scope(clause, node_id, language))
            facts["clauses"].append(
                {
                    "clause": clause,
                    "matches_node": None if hits is None else hits > 0,
                }
            )

    facts["failing_clauses"] = [c["clause"] for c in facts["clauses"] if c["matches_node"] is False]
    # Every probe failing means the query itself is rejected, not that the node
    # is a poor match - the report must not blame the document for that.
    facts["query_rejected_by_repository"] = bool(facts["probe_errors"]) and not facts["clauses"]
    return facts


# Properties Alfresco indexes untokenised, where a phrase must match whole.
UNTOKENISED_SUFFIXES = ("mimetype", "encoding", "creator", "modifier", "author")


def analyse_tokenisation(
    telemetry: Telemetry,
    failing_clauses: list[str],
    node_data: dict[str, Any],
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Show how Elasticsearch splits a failing clause, and the value it stores.

    A phrase is matched against tokens, not against the string that was typed,
    so `"s3-oidc-connector_v2"` becomes several terms and stops matching as a
    unit. Comparing the two token lists turns "looks identical but will not
    match" into something the report can point at.
    """
    properties = node_data.get("properties") or {}
    findings: list[dict[str, Any]] = []

    for clause in failing_clauses[:limit]:
        parsed = afts.field_and_value(clause)
        if not parsed:
            continue
        field, value = parsed
        wanted = value.strip("*")
        if not wanted or wanted != value:
            continue  # a wildcard clause is not matched as a phrase

        query_side = telemetry.analyze(wanted)
        if query_side.get("error"):
            continue

        stored = properties.get(field) or properties.get(field.lstrip("@"))
        stored_side = telemetry.analyze(str(stored)) if stored else {}

        tokens = query_side.get("tokens", [])
        stored_tokens = stored_side.get("tokens") or []
        untokenised = field.lower().endswith(UNTOKENISED_SUFFIXES)
        findings.append(
            {
                "clause": clause,
                "field": field,
                "queryValue": wanted,
                "queryTokens": tokens,
                "storedValue": stored,
                "storedTokens": stored_tokens,
                "splitsIntoSeveralTerms": len(tokens) > 1,
                "untokenisedField": untokenised,
                "tokensPresentInStored": bool(tokens)
                and all(token in stored_tokens for token in tokens),
            }
        )
    return findings


def _terms(query: str, language: str) -> list[str]:
    """Top-level terms, however the language marks them."""
    if language == "lucene":
        return lucene.top_level_terms(query)
    return afts.top_level_terms(query)


def _split_conjunction(query: str, language: str) -> list[str]:
    """Clauses safe to probe individually, or the query whole when unsound."""
    if language == "lucene":
        return lucene.split_top_level_and(query)
    return afts.split_top_level_and(query)


def _is_pure_conjunction(query: str, language: str) -> bool:
    if language == "lucene":
        return lucene.is_pure_conjunction(query)
    return afts.is_pure_conjunction(query)


def _scope(query: str, node_id: str, language: str) -> str:
    return (
        lucene.scope_to_node(query, node_id)
        if language == "lucene"
        else afts.scope_to_node(query, node_id)
    )


def analyse_empty_result(
    client: AlfrescoClient,
    afts_query: str,
    limit: int = MAX_CLAUSE_PROBES,
    language: str = "afts",
) -> list[dict[str, Any]]:
    """Count hits for each top-level term alone, to expose the dead one.

    A zero-hit search is otherwise indistinguishable from a wrong query; this
    turns it into "term 3 matches nothing on its own". Terms are measured
    individually, so this holds whatever operator joins them - which matters,
    because a query mixing AND and OR is exactly when the answer is least
    obvious.
    """
    clauses = _terms(afts_query, language)
    if len(clauses) < 2:
        return []

    counts: list[dict[str, Any]] = []
    for clause in clauses[:limit]:
        try:
            count = client.count_matches(clause, language=language)
            hits, exact = count.value, count.exact
        except AlfrescoError:
            hits, exact = None, True
        counts.append({"clause": clause, "hits": hits, "exact": exact})
    return counts


def analyse_conflict(
    client: AlfrescoClient, afts_query: str, limit: int = 6, language: str = "afts"
) -> list[dict[str, Any]]:
    """Find the clause that empties a conjunction whose parts all match.

    Knowing each clause matches something does not say which combination
    fails. Clauses are re-added one at a time until the running count hits
    zero, which names the exact pairing that conflicts.
    """
    clauses = _split_conjunction(afts_query, language)
    if len(clauses) < 2:
        return []

    # Lucene terms are already required, so they are joined by juxtaposition.
    joiner = " " if language == "lucene" else " AND "
    steps: list[dict[str, Any]] = []
    accumulated: list[str] = []
    for clause in clauses[:limit]:
        accumulated.append(clause)
        try:
            count = client.count_matches(joiner.join(accumulated), language=language)
            hits, exact = count.value, count.exact
        except AlfrescoError:
            return steps
        steps.append({"clause": clause, "hits": hits, "exact": exact})
        if hits == 0:
            break  # the first zero is the conflict; later clauses cannot revive it
    return steps


def analyse_path_reach(
    client: AlfrescoClient, afts_query: str, limit: int = 6, language: str = "afts"
) -> list[dict[str, Any]]:
    """Walk a PATH back up its ancestors to find the step that stops matching.

    A mistyped or invented folder name produces zero hits with no clue which
    part was wrong - and a single-clause query has no clause reach to fall
    back on. PATH syntax is shared, so only the probe language changes.
    """
    values = afts.path_values(afts_query)
    if not values:
        return []

    # A bare Lucene term is optional, which would make every probe match.
    required = "+" if language == "lucene" else ""
    reach: list[dict[str, Any]] = []
    for prefix in afts.path_prefixes(values[0])[:limit]:
        try:
            count = client.count_matches(f'{required}PATH:"{prefix}"', language=language)
            hits, exact = count.value, count.exact
        except AlfrescoError:
            hits, exact = None, True
        reach.append({"path": prefix, "hits": hits, "exact": exact})
    return reach


def analyse_path_containment(
    client: AlfrescoClient,
    afts_query: str,
    node_id: str,
    node_data: dict[str, Any],
    language: str = "afts",
) -> dict[str, Any] | None:
    """Whether the node actually sits inside the PATH the query asked for.

    Path reach proves the folder exists; it says nothing about where this
    document lives. Probing the PATH clause against the node alone separates
    "that folder is wrong" from "the document is somewhere else".
    """
    values = afts.path_values(afts_query)
    if not values:
        return None

    wanted = values[0]
    # PATH is spelled the same in both languages; only the scoping differs.
    clause = f'+PATH:"{wanted}"' if language == "lucene" else f'PATH:"{wanted}"'
    try:
        inside = (
            client.count_matches(_scope(clause, node_id, language), language=language).value > 0
        )
    except AlfrescoError:
        return None
    return {
        "queryPath": wanted,
        "nodePath": (node_data.get("path") or {}).get("name"),
        "nodeIsInsideQueryPath": inside,
    }


def load_node(client: AlfrescoClient, node_id: str) -> dict[str, Any]:
    try:
        return client.get_node_details(node_id)
    except AlfrescoError as exc:
        return {"_unreadable": True, "_error": str(exc), "_status": exc.status_code}


def load_parent_acl(client: AlfrescoClient, node_data: dict[str, Any]) -> dict[str, Any] | None:
    """A node usually inherits its restriction, so the blocker is the parent."""
    parent_id = node_data.get("parentId")
    if not parent_id:
        return None
    parent = load_node(client, parent_id)
    if parent.get("_unreadable"):
        return parent
    return {
        "id": parent.get("id"),
        "name": parent.get("name"),
        "path": parent.get("path", {}).get("name"),
        "permissions": parent.get("permissions", {}),
        **summarise_acl(parent),
    }


def summarise_acl(node_data: dict[str, Any]) -> dict[str, Any]:
    """Pull the two ACL flags out of the permissions block as plain fields.

    `isInheritanceEnabled` true is the normal case and proves nothing on its
    own, so the locally set entries are surfaced alongside it rather than left
    buried for a reader to find.
    """
    permissions = node_data.get("permissions") or {}
    locally_set = permissions.get("locallySet") or []
    return {
        "isInheritanceEnabled": permissions.get("isInheritanceEnabled"),
        "locallySet": locally_set,
        "locallySetAuthorities": sorted(
            {entry.get("authorityId") for entry in locally_set if entry.get("authorityId")}
        ),
        "inheritedAuthorities": sorted(
            {
                entry.get("authorityId")
                for entry in permissions.get("inherited") or []
                if entry.get("authorityId")
            }
        ),
    }


CONTENT_CLAUSE_MARKERS = ("text:", "cm:content")


def pipeline_is_implicated(facts: dict[str, Any]) -> bool:
    """Whether the indexing pipeline, rather than the query, is worth tracing.

    Tracing costs several calls across ES, the indexer and the broker, so it is
    reserved for the cases it can actually explain.
    """
    if facts.get("query_rejected_by_repository") or facts.get("node_is_deleted"):
        return False  # a query fault or a trashcan node needs no telemetry
    if facts.get("node_visible_to_user") is not True:
        return True  # absent from the index: exactly what the tracer explains
    # Metadata matched but a full-text clause did not: suspect extraction.
    failing = " ".join(facts.get("failing_clauses", [])).lower()
    return any(marker in failing for marker in CONTENT_CLAUSE_MARKERS)


def diagnose(
    client: AlfrescoClient,
    afts_query: str,
    node_reference: str,
    inspector: AlfrescoClient | None = None,
    telemetry: Telemetry | None = None,
) -> dict[str, Any]:
    """Probe as `client`; optionally read metadata as `inspector` (an admin).

    An ACL-blocked node returns 403 to the searching user, so without elevated
    credentials there is no permission tree to point at in the report.
    """
    node_id = (inspector or client).resolve_node_id(node_reference)
    language = client.detect_query_language()
    facts = collect_facts(client, afts_query, node_id, language=language)

    node_data = load_node(client, node_id)
    facts["node_readable_by_searching_user"] = not node_data.get("_unreadable", False)
    inspected_as = client.username

    if node_data.get("_unreadable") and inspector is not None:
        elevated = load_node(inspector, node_id)
        if not elevated.get("_unreadable"):
            node_data = elevated
            inspected_as = inspector.username
    facts["metadata_inspected_as"] = inspected_as
    facts["node_acl"] = summarise_acl(node_data)
    facts["path_containment"] = analyse_path_containment(
        client, afts_query, node_id, node_data, language=language
    )

    parent_acl = load_parent_acl(inspector or client, node_data)

    # Tokenisation only explains a clause that looked like it should match.
    if telemetry is not None and facts.get("failing_clauses"):
        facts["tokenisation"] = analyse_tokenisation(telemetry, facts["failing_clauses"], node_data)

    pipeline: dict[str, Any] | None = None
    if telemetry is not None and pipeline_is_implicated(facts):
        try:
            pipeline = trace_node(inspector or client, node_id, telemetry, afts_query)
        except (AlfrescoError, ValueError) as exc:
            log.info("pipeline trace unavailable for %s: %s", node_id, exc)
    facts["pipeline_traced"] = pipeline is not None

    pipeline_block = (
        json.dumps(pipeline, indent=2, default=str)
        if pipeline
        else "Not collected: the probe results point at the query, not the index."
    )

    user_message = f"""AFTS query under test:
{afts_query}

Searching user: {client.username}
Node metadata below was read as: {inspected_as}

DETERMINISTIC PROBE RESULTS:
{json.dumps(facts, indent=2)}

NODE METADATA, ASPECTS, PATH AND PERMISSIONS:
{json.dumps(node_data, indent=2, default=str)}

PARENT FOLDER ACL:
{json.dumps(parent_acl, indent=2, default=str)}

PIPELINE EVIDENCE:
{pipeline_block}
"""
    report = llm.complete(DIAGNOSTIC_SYSTEM_PROMPT, user_message, temperature=0.1)
    return {
        "node_id": node_id,
        "facts": facts,
        "node": node_data,
        "parent_acl": parent_acl,
        "pipeline": pipeline,
        "inspected_as": inspected_as,
        "report": report,
    }
