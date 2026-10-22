"""Lucene query utilities, as Alfresco's search API accepts them.

Alfresco's Lucene dialect overlaps AFTS but is not the same language: terms
carry a `+`/`-` occurrence prefix instead of AND/OR keywords, and a property
name escapes its colon (`@cm\\:name`). Clause isolation therefore needs its
own splitter, even though the two languages share PATH values, quoting and
the `ID:` field - which is why the diagnostics can be reused unchanged.
"""

from __future__ import annotations

import re

from alfresco_curator.alfresco.afts import CLOSERS, OPENERS, node_ref

# `@cm\:name:"x"` - the escaped colon separates the prefix from the local
# name, and a bare field such as `TYPE:"cm:content"` has no local part.
ESCAPED_FIELD = re.compile(r'^@?([A-Za-z_][\w.]*)\\?:(?:([\w.]*)\s*:)?\s*"?(.*?)"?$')


def scope_to_node(query: str, node_id: str) -> str:
    """Restrict a Lucene query to one node.

    Alfresco's Lucene parser accepts the AND keyword and the ID field, so the
    same construction works here as for AFTS.
    """
    return f'({query}) AND ID:"{node_ref(node_id)}"'


def strip_occurrence(term: str) -> str:
    """Drop the leading +/- so a term can be read as a field clause."""
    return term.lstrip("+-").strip()


def top_level_terms(query: str) -> list[str]:
    """Split a Lucene query into its top-level terms.

    Terms are whitespace separated and may carry a `+`/`-` prefix, so the
    split respects quotes and brackets but does not look for keywords. An
    AND/OR keyword is skipped rather than treated as a term, which keeps the
    hybrid syntax Alfresco tolerates from producing phantom clauses.
    """
    terms: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    text = query or ""

    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            buf.append(text[i : i + 2])
            i += 2
            continue
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in OPENERS:
            depth += 1
        elif ch in CLOSERS:
            depth = max(0, depth - 1)

        if depth == 0 and ch.isspace():
            terms.append("".join(buf))
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    terms.append("".join(buf))
    operators = {"AND", "OR", "NOT"}
    return [t for t in (term.strip() for term in terms) if t and t.upper() not in operators]


def is_pure_conjunction(query: str) -> bool:
    """True when every term is required, so clauses may be isolated soundly.

    A bare term is optional in Lucene (SHOULD, not MUST), so only a query
    whose terms all carry '+' can be taken apart clause by clause without
    changing what it means.
    """
    terms = top_level_terms(query)
    return bool(terms) and all(term.startswith("+") for term in terms)


def split_top_level_and(query: str) -> list[str]:
    """Clauses to probe individually, or the whole query when that is unsound."""
    terms = top_level_terms(query)
    if not is_pure_conjunction(query) or len(terms) <= 1:
        return [query.strip()]
    return terms


def field_and_value(term: str) -> tuple[str, str] | None:
    """The property and value a Lucene term constrains, colon-escapes removed."""
    match = ESCAPED_FIELD.match(strip_occurrence(term))
    if not match:
        return None
    prefix, local, value = match.groups()
    field = f"{prefix}:{local}" if local else prefix
    return field.lower(), value.lower()
