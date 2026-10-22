"""Natural language and raw AFTS search."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

from alfresco_curator.alfresco import afts
from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.diagnostics.query_generator import generate_query
from alfresco_curator.diagnostics.search_debugger import (
    analyse_conflict,
    analyse_empty_result,
    analyse_path_reach,
)
from alfresco_curator.errors import AlfrescoError, LLMError, SessionExpiredError
from alfresco_curator.web.dependencies import Searcher
from alfresco_curator.web.schemas import (
    ClauseCount,
    ConflictStep,
    PathReach,
    RawSearchRequest,
    SearchHit,
    SearchRequest,
    SearchResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/search", tags=["search"])


def _to_hit(entry: dict[str, Any]) -> SearchHit:
    properties = entry.get("properties") or {}
    content = entry.get("content") or {}
    return SearchHit(
        id=entry.get("id"),
        name=entry.get("name"),
        nodeType=entry.get("nodeType"),
        path=(entry.get("path") or {}).get("name"),
        title=properties.get("cm:title"),
        description=properties.get("cm:description"),
        modifiedAt=entry.get("modifiedAt"),
        mimeType=content.get("mimeType"),
    )


def _execute(
    client: AlfrescoClient,
    query: str,
    max_items: int,
    info: dict[str, Any],
    language: str = "afts",
) -> SearchResponse:
    try:
        results = client.search_afts(query, max_items=max_items, language=language)
    except SessionExpiredError:
        raise  # handled globally as a 401 so the UI can re-authenticate
    except AlfrescoError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    listing = results.get("list", {})
    entries = listing.get("entries", [])

    # `totalItems` is derived from the page just returned, so it reads
    # "26" for a query matching half a million documents. When rows remain,
    # a full-page count probe resolves the real figure, or an honest floor.
    pagination = listing.get("pagination", {})
    if pagination.get("hasMoreItems"):
        try:
            count = client.count_matches(query, language=language)
            total, total_is_exact = count.value, count.exact
        except AlfrescoError:
            total, total_is_exact = int(pagination.get("totalItems", 0)) - 1, False
    else:
        total, total_is_exact = int(pagination.get("totalItems", 0)), True

    # Zero hits is the ambiguous case: report which clause is over-constraining
    # rather than leaving it indistinguishable from an empty repository.
    clause_counts = (
        [ClauseCount(**row) for row in analyse_empty_result(client, query, language=language)]
        if total == 0
        else []
    )
    # A folder query is often a single clause, so it has no clause reach to
    # fall back on; walking the path back up shows which step is wrong.
    path_reach = (
        [PathReach(**row) for row in analyse_path_reach(client, query, language=language)]
        if total == 0
        else []
    )
    # Clauses that all match individually still say nothing about which
    # combination of them is empty, so narrow down to the one that breaks it.
    conflict = (
        [ConflictStep(**row) for row in analyse_conflict(client, query, language=language)]
        if total == 0 and clause_counts and all(row.hits for row in clause_counts)
        else []
    )

    return SearchResponse(
        aftsQuery=query,
        language=language,
        explanation=info.get("explanation", ""),
        assumptions=info.get("assumptions", []),
        warnings=info.get("warnings", []),
        fieldsUsed=info.get("fields_used", []),
        total=total,
        totalIsExact=total_is_exact,
        results=[_to_hit(item["entry"]) for item in entries],
        clauseCounts=clause_counts,
        pathReach=path_reach,
        conflict=conflict,
    )


@router.post("", response_model=SearchResponse)
def search(body: SearchRequest, client: Searcher) -> SearchResponse:
    language = client.detect_query_language()
    try:
        info = generate_query(body.prompt, language=language)
    except LLMError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return _execute(client, info["afts_query"], body.max_items, info, language)


@router.post("/raw", response_model=SearchResponse)
def search_raw(body: RawSearchRequest, client: Searcher) -> SearchResponse:
    language = client.detect_query_language()
    # Only AFTS has a structural checker; Lucene is left to the repository.
    problems = afts.syntax_problems(body.query) if language == "afts" else []
    if problems:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="; ".join(problems))
    return _execute(client, body.query, body.max_items, {}, language)
