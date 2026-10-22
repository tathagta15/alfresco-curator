"""AFTS query utilities: top-level clause splitting and node-scoped probes."""

from __future__ import annotations

import re

OPENERS = "([{"
CLOSERS = ")]}"

# Node ids are interpolated into a quoted AFTS term, so anything that could
# close the quote or the term is rejected rather than escaped.
NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")


def node_ref(node_id: str) -> str:
    if not NODE_ID.match((node_id or "").strip()):
        raise ValueError(f"unsafe or malformed node id: {node_id!r}")
    return f"workspace://SpacesStore/{node_id.strip()}"


def scope_to_node(afts_query: str, node_id: str) -> str:
    """Restrict a query to a single node so a hit/miss is unambiguous."""
    return f'({afts_query}) AND ID:"{node_ref(node_id)}"'


def is_pure_conjunction(afts_query: str) -> bool:
    return all(op in ("AND", "") for _, op in _tokenize(afts_query))


def mixes_top_level_operators(afts_query: str) -> bool:
    """True when AND and OR sit side by side at the top level.

    AND binds tighter than OR, so `A AND B OR C AND D` means `(A AND B) OR
    (C AND D)` - almost never what was asked for, and it silently returns the
    wrong set rather than failing.
    """
    operators = {op for _, op in _tokenize(afts_query) if op}
    return "AND" in operators and "OR" in operators


def syntax_problems(afts_query: str) -> list[str]:
    """Catch the malformed queries that the repository reports only as a 400."""
    query = (afts_query or "").strip()
    if not query:
        return ["query is empty"]

    problems: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(query):
        ch = query[i]
        if quote is not None:
            if ch == "\\" and i + 1 < len(query):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(query):
            i += 2
            continue
        if ch in "\"'":
            quote = ch
        elif ch in OPENERS:
            depth += 1
        elif ch in CLOSERS:
            depth -= 1
            if depth < 0:
                problems.append("unbalanced closing bracket")
                depth = 0
        i += 1

    if quote is not None:
        problems.append(f"unterminated {quote} quote")
    if depth > 0:
        problems.append(f"{depth} unclosed bracket(s)")

    upper = query.upper()
    for operator in ("AND", "OR", "NOT"):
        if upper.endswith(f" {operator}") or upper == operator:
            problems.append(f"query ends with a dangling {operator}")
        if upper.startswith(f"{operator} "):
            problems.append(f"query starts with a dangling {operator}")
    return problems


PATH_VALUE = re.compile(r'PATH:"([^"]+)"')


def path_values(afts_query: str) -> list[str]:
    return PATH_VALUE.findall(afts_query or "")


def path_prefixes(path_value: str) -> list[str]:
    """Ancestor paths, deepest first, each as a descendant query.

    A folder query that matches nothing is usually wrong at one specific step;
    walking back up finds where it stops resolving.
    """
    core = (path_value or "").strip()
    while core.endswith(("*", "/")):
        core = core[:-1]
    steps = [step for step in core.split("/") if step]
    return ["/" + "/".join(steps[:depth]) + "//*" for depth in range(len(steps), 0, -1)]


def top_level_terms(afts_query: str) -> list[str]:
    """Every top-level term, whatever operator joins them.

    Unlike `split_top_level_and` this makes no soundness claim about the
    combination; it is for measuring how far each term reaches on its own.
    """
    return [clause for clause, _ in _tokenize(afts_query)]


FIELD_VALUE = re.compile(r'^[=~]?@?([A-Za-z_][\w.:]*)\s*:\s*"?(.*?)"?$')


def field_and_value(clause: str) -> tuple[str, str] | None:
    match = FIELD_VALUE.match(clause.strip())
    return (match.group(1).lower(), match.group(2).lower()) if match else None


def _or_groups(afts_query: str) -> list[str]:
    """The query itself plus every bracketed sub-expression, OR-joined only."""
    query = afts_query or ""
    candidates = [query]
    starts: list[int] = []
    quote: str | None = None
    i = 0
    while i < len(query):
        ch = query[i]
        if ch == "\\":
            i += 2
            continue
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            starts.append(i)
        elif ch == ")" and starts:
            candidates.append(query[starts.pop() + 1 : i])
        i += 1
    return [c for c in candidates if {op for _, op in _tokenize(c) if op} == {"OR"}]


# A node holds exactly one of these, so alternatives can only ever be ORed -
# "spreadsheets and CSV files" is unsatisfiable as a conjunction.
SINGLE_VALUED = frozenset(
    {
        "type",
        "exacttype",
        "cm:content.mimetype",
        "cm:content.encoding",
        "id",
        "parent",
        "primaryparent",
    }
)


def broadening_or_groups(afts_query: str) -> list[str]:
    """OR groups that widen the request rather than offering alternate places.

    `cm:name:"*x*" OR cm:title:"*x*"` looks for one thing in two fields, which
    is what the user asked for. `TEXT:"expenses" OR TEXT:"policy"` is the
    opposite: the same field with different values matches anything about
    either subject, so a two-word request returns documents that satisfy only
    half of it.
    """
    groups: list[str] = []
    for candidate in _or_groups(afts_query):
        branches = [field_and_value(clause) for clause, _ in _tokenize(candidate)]
        if len(branches) < 2 or any(branch is None for branch in branches):
            continue
        fields = {field for field, _ in branches}  # type: ignore[misc]
        values = {value for _, value in branches}  # type: ignore[misc]
        if fields & SINGLE_VALUED:
            continue
        if len(fields) == 1 and len(values) == len(branches):
            groups.append(candidate.strip())
    return groups


def split_top_level_and(afts_query: str) -> list[str]:
    """Split on top-level AND only. Quoted strings, escapes, brackets are respected.

    Clause isolation is only sound for a pure conjunction, so a query with a
    top-level OR or AND NOT is returned as a single opaque clause.
    """
    tokens = _tokenize(afts_query)
    if not is_pure_conjunction(afts_query) or len(tokens) <= 1:
        return [afts_query.strip()]
    return [clause for clause, _ in tokens]


def _tokenize(afts_query: str) -> list[tuple[str, str]]:
    """Return [(clause, operator_following_it)] for top-level AND/OR boundaries."""
    clauses: list[tuple[str, str]] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    n = len(afts_query)

    while i < n:
        ch = afts_query[i]

        if quote is not None:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(afts_query[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue

        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(afts_query[i + 1])
            i += 2
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
            for op in ("AND NOT", "AND", "OR"):
                candidate = afts_query[i:]
                if _matches_operator(candidate, op):
                    clauses.append(("".join(buf).strip(), op))
                    buf = []
                    i += len(op) + 1
                    break
            else:
                buf.append(ch)
                i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        clauses.append((tail, ""))
    return [(c, op) for c, op in clauses if c]


def _matches_operator(text: str, op: str) -> bool:
    """True when `text` starts with ' <OP> ' (surrounding whitespace required)."""
    if not text[:1].isspace():
        return False
    candidate = text[1 : 1 + len(op)]
    if candidate != op:
        return False
    rest = text[1 + len(op) :]
    return bool(rest) and rest[0].isspace()
