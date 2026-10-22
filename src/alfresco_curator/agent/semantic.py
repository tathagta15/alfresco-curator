"""Finding documents that share a meaning with the request but not a word.

"Where is the vulnerability review" returns nothing, while
`Penetration_Test_Report_2026.pdf` sits in the repository being exactly that.
Full text search is lexical: it matches the words the author happened to use,
and the person asking rarely knows them.

The honest description of what this does is *concept expansion*, not vector
search. There is no embedding service in this deployment - the gateway answers
404 for `/embeddings` - so instead the model is asked, once, for the vocabulary
a document of this kind would actually contain, and those terms are searched
as alternatives. The result is the same for the user and the mechanism is
cheaper, but it is not a nearest-neighbour index and is not claimed to be.

It runs only after a lexical search has found nothing, so the common case
costs no extra call.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from alfresco_curator.errors import LLMError
from alfresco_curator.llm import client as llm

log = logging.getLogger(__name__)

MAX_TERMS = 8

_SYSTEM = """You expand a failed document search into the words such a document
would really contain.

Return JSON: {"terms": ["...", "..."]}

Rules:
- Terms are what a filing clerk would have typed: document kinds, synonyms,
  industry names, common abbreviations.
- A "vulnerability review" is filed as a penetration test, a security
  assessment, a pentest report, an audit.
- No boolean syntax, no field names, no quotes, no wildcards. Words only.
- At most 8, ordered by how likely they are to appear.
- If the request is already the natural wording, return it unchanged."""


def expand(prompt: str) -> list[str]:
    """Vocabulary a matching document would plausibly use."""
    try:
        reply = llm.complete_json(_SYSTEM, prompt)
    except (LLMError, Exception) as exc:
        log.info("concept expansion unavailable: %s", exc)
        return []

    terms = reply.get("terms") if isinstance(reply, dict) else None
    if not isinstance(terms, list):
        return []

    cleaned: list[str] = []
    for term in terms:
        if not isinstance(term, str):
            continue
        # Quotes and backslashes would close or escape the phrase they sit in,
        # turning a term into syntax.
        word = re.sub(r'["\\]', " ", term).strip()
        word = re.sub(r"\s+", " ", word)
        if word and word.lower() not in {w.lower() for w in cleaned}:
            cleaned.append(word)
    return cleaned[:MAX_TERMS]


def as_query(terms: list[str]) -> str:
    """One OR across the expanded vocabulary.

    OR rather than AND because these are alternative wordings of the same
    idea; requiring all of them would be stricter than the query that already
    failed.
    """
    if not terms:
        return ""
    return " OR ".join(f'TEXT:"{term}"' for term in terms)


_RANK_SYSTEM = """You decide which documents answer a request.

You get the request and a numbered list of candidate documents, each with a
name and a path. Return JSON: {"keep": [1, 4]}

Rules:
- Keep a candidate only if a person who asked this would be glad to see it.
- A document that merely shares a common word is not an answer. "Rules for
  working from home" is not answered by an email template that contains the
  word "rules".
- System, template and configuration files are almost never the answer to a
  question about business content.
- Keep nothing rather than keep filler. An empty list is a valid answer.
- Judge by name and path only. Do not invent documents."""


def rank(prompt: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The candidates that actually answer the request.

    A relaxed query matches by construction - it was built by dropping
    whatever did not match - so "it returned rows" is not evidence that it
    returned answers. Dropping `TEXT:"working from home"` leaves `TEXT:"rules"`,
    which eight email templates satisfy and none of them answer.

    Unavailable model means the candidates pass through unjudged: filtering is
    an improvement, not a gate.
    """
    if not candidates:
        return []

    listing = "\n".join(
        f"{i + 1}. {c.get('name', '?')}  [{c.get('path', '')}]"
        for i, c in enumerate(candidates[:20])
    )
    try:
        reply = llm.complete_json(_RANK_SYSTEM, f"Request: {prompt}\n\nCandidates:\n{listing}")
    except Exception as exc:
        log.info("ranking unavailable, keeping candidates unjudged: %s", exc)
        return candidates

    keep = reply.get("keep") if isinstance(reply, dict) else None
    if not isinstance(keep, list):
        return candidates

    chosen = []
    for position in keep:
        if isinstance(position, int) and 1 <= position <= len(candidates):
            chosen.append(candidates[position - 1])
    return chosen


def search(client: Any, prompt: str, *, language: str, max_items: int = 5) -> dict[str, Any]:
    """Expand, search, and report what was tried.

    The terms are returned whatever the outcome: when this finds nothing
    either, knowing which vocabulary was ruled out is the useful part.
    """
    terms = expand(prompt)
    query = as_query(terms)
    if not query:
        return {"terms": [], "query": "", "total": 0, "results": []}

    try:
        payload = client.search_afts(query, max_items=max_items, language=language)
    except Exception as exc:
        log.info("concept search failed: %s", exc)
        return {"terms": terms, "query": query, "total": 0, "results": []}

    listing = payload.get("list", {})
    return {
        "terms": terms,
        "query": query,
        "total": listing.get("pagination", {}).get("totalItems", 0),
        "results": [
            {
                "id": item["entry"].get("id"),
                "name": item["entry"].get("name"),
                "path": (item["entry"].get("path") or {}).get("name"),
            }
            for item in listing.get("entries", [])
        ],
    }
