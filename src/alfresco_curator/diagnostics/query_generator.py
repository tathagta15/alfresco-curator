"""Natural language -> Alfresco Full Text Search (AFTS) translation."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from alfresco_curator import llm
from alfresco_curator.alfresco import afts, lucene
from alfresco_curator.errors import LLMError

AFTS_SYSTEM_PROMPT = """You are an expert Alfresco Full Text Search (AFTS) architect.
Translate a natural language request into a single, valid AFTS query.

AFTS syntax rules:
1. Free text:        TEXT:"quarterly report"   (also: cm:name:"budget")
2. Properties:       =cm:name:"exact"  |  cm:name:"*spec*"  |  @cm:title:"S3"
   - The '=' prefix forces an exact, untokenised match.
   - The '~' prefix requests fuzzy/expanded matching.
   - Property names may be written cm:name or @cm:name; both are valid.
3. Types:            TYPE:"cm:content"  TYPE:"cm:folder"  (TYPE matches subtypes;
                     use EXACTTYPE:"cm:content" to exclude subtypes)
4. Aspects:          ASPECT:"cm:titled"  ASPECT:"cm:versionable"
5. Paths:            PATH:"/app:company_home/st:sites/cm:demo//*"
                     '//*' = any descendant, '/*' = immediate children only.
6. Ranges:           cm:modified:[2026-08-01T00:00:00.000Z TO NOW]
                     cm:content.size:[1048576 TO MAX]
7. Content fields:   cm:content.mimetype:"application/pdf"
                     cm:content.size, cm:content.encoding
8. Booleans:         AND, OR, NOT must be uppercase. Group with parentheses.
   ALWAYS parenthesise an OR group when it sits alongside an AND. AND binds
   tighter than OR, so `TYPE:"cm:content" AND a:"x" OR a:"y"` means
   `(TYPE AND a:"x") OR a:"y"`, which is not what the user asked. Write
   `TYPE:"cm:content" AND (a:"x" OR a:"y")` instead.
9. Wildcards:        '*' multi-char, '?' single char. Leading wildcards are
   supported but slow. Wildcards do NOT work inside '=' exact matches.
10. Prefer mimetype over filename extension when the user says "PDFs", and add
    TYPE:"cm:content" whenever the user is asking for documents/files.

Hard constraints:
- Emit ONE query string. No line breaks, no trailing operators.
- Use double quotes around values. Do not escape the ':' inside property names.
- Dates must be ISO-8601 UTC with milliseconds, or the literals NOW / MIN / MAX.
- Never invent custom models (e.g. acme:status) unless the user names them.

Respond with JSON only:
{
  "afts_query": "...",
  "explanation": "one or two sentences, plain English",
  "assumptions": ["any guess you had to make"],
  "fields_used": ["cm:name", "cm:modified"]
}

PATH is the clause most often written wrongly. A PATH value is a sequence of
namespaced steps, each `prefix:Name`, separated by single slashes. A wildcard
is only ever the LAST step: `/*` for immediate children, `//*` for any
descendant. A wildcard may never appear before a named step.
  CORRECT   PATH:"/app:company_home/cm:Engineering//*"
  CORRECT   PATH:"/app:company_home/st:sites/cm:demo/cm:documentLibrary/*"
  WRONG     PATH:"/app:company_home//*cm:Engineering//*"
  WRONG     PATH:"/Company Home/Engineering"
Top-level folders sit directly under `/app:company_home`, so a folder named
Engineering is `/app:company_home/cm:Engineering`. Spaces in a folder name are
written `_x0020_`.

Worked examples:

User: approved S3 connector specifications in the Engineering folder
{"afts_query": "TYPE:\"cm:content\" AND cm:title:\"*S3*\" AND cm:description:\"*APPROVED*\" AND PATH:\"/app:company_home/cm:Engineering//*\"",
 "explanation": "Content items under Engineering whose title mentions S3 and whose description is marked APPROVED.",
 "assumptions": ["'approved' is recorded in cm:description rather than a custom status property"],
 "fields_used": ["cm:title", "cm:description"]}

User: PDFs changed in the last 30 days
{"afts_query": "TYPE:\"cm:content\" AND cm:content.mimetype:\"application/pdf\" AND cm:modified:[NOW/DAY-30DAYS TO NOW]",
 "explanation": "PDF content modified within the last 30 days.",
 "assumptions": [],
 "fields_used": ["cm:content.mimetype", "cm:modified"]}

User: folders called archive
{"afts_query": "TYPE:\"cm:folder\" AND cm:name:\"*archive*\"",
 "explanation": "Folders whose name contains 'archive'.",
 "assumptions": ["partial name match intended"],
 "fields_used": ["cm:name"]}

Prefer specific property clauses over TEXT when the user refers to names,
titles, statuses, types or locations. Use TEXT only for genuine free-text
content searches.

Never put a long multi-word phrase inside TEXT. `TEXT:"approved S3 connector
specifications"` is an exact phrase match and will almost always return
nothing. Decompose the request into the specific fields it implies - a status
word belongs in cm:description or a status property, a subject word in
cm:title or cm:name, a location in PATH. If free text is genuinely needed,
use at most two or three words, ANDed as separate TEXT terms.

Every word the user wrote is a requirement, so join distinct words with AND.
OR is ONLY for looking for the SAME value in different places, never for
different values in the same field:
  CORRECT  (cm:name:"*Northwind*" OR cm:title:"*Northwind*")   one thing, two fields
  WRONG    (TEXT:"expenses" OR TEXT:"policy")                  matches either word alone
  CORRECT  TEXT:"expenses" AND TEXT:"policy"
Do not widen a request to avoid returning nothing. An empty result that
faithfully reflects the question is more useful than a large one that does
not, because the caller explains empty results back to the user.

Joining with AND does not mean one clause per word. Metadata holds the
DISTINGUISHING terms, not every word of a description, so a clause built from
a generic noun - specifications, documents, files, items, reports, records,
details, information - almost always matches nothing and kills the whole
query. Keep the proper nouns, product names, codes and status words; drop the
filler. For "approved S3 connector specifications", the identifying term is
S3 and the status is approved; "connector" and "specifications" are
description, not metadata. Aim for three or four clauses, not six.

User: expenses policy
{"afts_query": "TYPE:\"cm:content\" AND TEXT:\"expenses\" AND TEXT:\"policy\"",
 "explanation": "Content items whose text contains both 'expenses' and 'policy'.",
 "assumptions": [],
 "fields_used": ["TEXT"]}
"""

LUCENE_SYSTEM_PROMPT = """You are an expert in Alfresco's Lucene query
language. Translate a natural language request into a single, valid Lucene
query as the Alfresco Search API accepts it.

This is Alfresco's Lucene dialect, NOT AFTS and NOT raw Elasticsearch:
1. Occurrence:       '+' means the term is REQUIRED, '-' means it is excluded.
   A bare term is merely optional, so almost every term should carry '+'.
   CORRECT  +TYPE:"cm:content" +@cm\\:title:"*S3*"
   WRONG    TYPE:"cm:content" @cm\\:title:"*S3*"     (both optional)
2. Properties:       a property is written @prefix\\:name - the colon INSIDE
   the property name is escaped with a backslash, and the unescaped colon
   that follows separates the field from its value.
   CORRECT  +@cm\\:description:"*APPROVED*"
   WRONG    +cm:description:"*APPROVED*"
3. Special fields:   TYPE, ASPECT, PATH, TEXT, ID are written WITHOUT the
   '@' and WITHOUT an escaped colon: +TYPE:"cm:content", +TEXT:"invoice".
4. Paths:            +PATH:"/app:company_home/cm:Engineering//*"
   '//*' = any descendant, '/*' = immediate children only. Identical to AFTS.
5. Grouping:         parentheses group alternatives, and OR inside a required
   group means "any of these":
   +TYPE:"cm:content" +(@cm\\:title:"*S3*" OR @cm\\:name:"*S3*")
6. Ranges:           +@cm\\:modified:[2026-08-01T00:00:00.000Z TO NOW]
7. Wildcards:        '*' multi-char, '?' single char.

Hard constraints:
- Emit ONE query string. No line breaks.
- Prefix every required term with '+'; use '-' for exclusions.
- Escape the colon in property names, and only there.
- Never invent custom models unless the user names them.

Respond with JSON only:
{
  "query": "...",
  "explanation": "one or two sentences, plain English",
  "assumptions": ["any guess you had to make"],
  "fields_used": ["cm:name", "cm:modified"]
}

Worked examples:

User: approved S3 connector specifications in the Engineering folder
{"query": "+TYPE:\\"cm:content\\" +@cm\\\\:title:\\"*S3*\\" +@cm\\\\:description:\\"*APPROVED*\\" +PATH:\\"/app:company_home/cm:Engineering//*\\"",
 "explanation": "Content under Engineering whose title mentions S3 and whose description is marked APPROVED.",
 "assumptions": ["'approved' is recorded in cm:description"],
 "fields_used": ["cm:title", "cm:description"]}

User: invoices for Northwind Traders
{"query": "+TYPE:\\"cm:content\\" +(@cm\\\\:name:\\"*Northwind*\\" OR @cm\\\\:title:\\"*Northwind*\\") +TEXT:\\"invoice\\"",
 "explanation": "Content mentioning invoice whose name or title contains Northwind.",
 "assumptions": [],
 "fields_used": ["cm:name", "cm:title"]}

User: documents that are not drafts
{"query": "+TYPE:\\"cm:content\\" -@cm\\\\:description:\\"*DRAFT*\\"",
 "explanation": "Content whose description is not marked DRAFT.",
 "assumptions": ["draft status is recorded in cm:description"],
 "fields_used": ["cm:description"]}

Every word the user wrote is a requirement, so give each its own '+' term.
OR is only for the SAME value in different fields, never for different values
in one field. Do not widen a request to avoid returning nothing - an empty
result that reflects the question is more useful, because the caller explains
empty results back to the user. Metadata holds the DISTINGUISHING terms, so
drop generic nouns such as documents, files, specifications or reports.
"""

PATH_CLAUSE = re.compile(r'PATH:"([^"]*)"')


def path_problems(afts_query: str) -> list[str]:
    """Structural check for the clause models most often corrupt.

    A malformed PATH is not rejected by the repository; it just silently
    matches nothing, which is indistinguishable from a genuine empty result.
    """
    problems: list[str] = []
    for value in PATH_CLAUSE.findall(afts_query):
        if not value.startswith("/"):
            problems.append(f'PATH "{value}" must start with /')
            continue
        steps = [s for s in value.split("/") if s]
        for index, step in enumerate(steps):
            is_last = index == len(steps) - 1
            if step == "*":
                if not is_last:
                    problems.append(f'PATH "{value}" has a wildcard before a named step')
            elif "*" in step:
                problems.append(f'PATH "{value}" mixes a wildcard into step "{step}"')
            elif ":" not in step:
                problems.append(f'PATH "{value}" step "{step}" is missing a namespace prefix')
    return problems


def generate_afts(user_prompt: str, *, extra_context: str = "") -> dict[str, Any]:
    return generate_query(user_prompt, language="afts", extra_context=extra_context)


def generate_query(
    user_prompt: str, *, language: str = "afts", extra_context: str = ""
) -> dict[str, Any]:
    """Translate a request into AFTS or Alfresco Lucene.

    Both languages share PATH syntax, so the structural PATH repair applies to
    either; the advisories are language-aware because the operators differ.
    """
    prompt = LUCENE_SYSTEM_PROMPT if language == "lucene" else AFTS_SYSTEM_PROMPT
    key = "query" if language == "lucene" else "afts_query"

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    user_message = f"Current UTC time: {now}\nUser request: {user_prompt}\n{extra_context}".strip()

    result = llm.complete_json(prompt, user_message)
    # Deliberately JSON mode rather than with_structured_output(): measured
    # against these prompts, the tool-calling path pays less attention to the
    # worked examples above and widens clauses that then match nothing.
    # A model that ignores the key name should not fail the whole request.
    query = str(result.get(key) or result.get("query") or result.get("afts_query") or "").strip()
    if not query:
        raise LLMError(f"Model returned no {language} query.")

    problems = path_problems(query)
    if problems:
        repair = (
            f"{user_message}\n\n"
            f"Your previous answer was {query}\n"
            f"It is invalid: {'; '.join(problems)}\n"
            "Rewrite it with a valid PATH built only from prefix:Name steps, "
            "with a wildcard as the final step at most."
        )
        retried = llm.complete_json(prompt, repair)
        repaired = str(
            retried.get(key) or retried.get("query") or retried.get("afts_query") or ""
        ).strip()
        if repaired and not path_problems(repaired):
            result, query, problems = retried, repaired, []

    return {
        "afts_query": query,
        "language": language,
        "explanation": result.get("explanation", ""),
        "assumptions": result.get("assumptions", []) or [],
        "fields_used": result.get("fields_used", []) or [],
        "warnings": problems + advisories(query, language=language),
    }


def advisories(afts_query: str, *, language: str = "afts") -> list[str]:
    """Valid queries that are probably not what was meant."""
    if language == "lucene":
        return _lucene_advisories(afts_query)

    warnings: list[str] = []
    if afts.mixes_top_level_operators(afts_query):
        warnings.append(
            "AND and OR are mixed at the top level without parentheses. AND "
            "binds tighter, so the OR branch is evaluated separately - group "
            "it in parentheses if that was not intended."
        )
    for group in afts.broadening_or_groups(afts_query):
        warnings.append(
            f"`{group}` matches any one of those values, not all of them, so "
            "this result set is wider than the request. Swap the OR for AND "
            "to require every term."
        )
    return warnings


def _lucene_advisories(query: str) -> list[str]:
    """Lucene's own trap: a term with no '+' is optional, not required."""
    warnings: list[str] = []
    terms = lucene.top_level_terms(query)
    optional = [t for t in terms if not t.startswith(("+", "-"))]
    if optional and len(terms) > 1:
        warnings.append(
            f"{', '.join(f'`{t}`' for t in optional)} "
            + ("is" if len(optional) == 1 else "are")
            + " missing the '+' prefix, so in Lucene "
            + ("it is" if len(optional) == 1 else "they are")
            + " optional rather than required - documents can match without "
            "satisfying them. Add '+' to make the term mandatory."
        )
    if any("@" in term and "\\:" not in term for term in terms):
        warnings.append(
            "A property written without the escaped colon (`@cm\\:name`) will "
            "not resolve in Lucene. Escape the colon inside the property name."
        )
    return warnings
