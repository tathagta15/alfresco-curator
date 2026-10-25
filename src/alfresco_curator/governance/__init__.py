"""Governance controls: what the agent decided, and what left the network.

Three record kinds, written to one hash-chained ledger:

- `ask`      the instruction and the route it was given, which is the only
             place "why did the AI do this" has an answer
- `egress`   which node ids and which *field names* were sent to which model
             host, so a record of processing can be produced on demand
- `decision` the proposed change, its outcome, and whether it can be put back
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from alfresco_curator.config import get_settings
from alfresco_curator.governance.ledger import GENESIS, Ledger
from alfresco_curator.governance.policy import (
    SENSITIVE_FIELDS,
    PolicyError,
    assert_endpoint_allowed,
    endpoint_for,
    host_of,
    minimise,
)

__all__ = [
    "GENESIS",
    "SENSITIVE_FIELDS",
    "Ledger",
    "PolicyError",
    "assert_endpoint_allowed",
    "endpoint_for",
    "get_ledger",
    "host_of",
    "minimise",
    "record_ask",
    "record_decision",
    "record_egress",
]


@lru_cache(maxsize=1)
def get_ledger() -> Ledger:
    return Ledger(get_settings().governance.ledger_path or None)


def record_ask(prompt: str, route: str, actor: str, session: str = "") -> None:
    """The instruction and the engine it reached.

    The prompt is kept whole: an audit that cannot show what was asked cannot
    explain what was done. It inherits the sensitivity of the content it
    governs, which is why the ledger belongs on a protected volume.
    """
    get_ledger().append("ask", actor, {"prompt": prompt, "route": route}, session)


def record_egress(
    purpose: str,
    actor: str,
    node_ids: Iterable[str],
    fields: Iterable[str],
    session: str = "",
) -> None:
    """What was transmitted to a model, named rather than copied.

    Field names and node ids only. An audit trail that copies the values it is
    auditing is simply a second place the personal data lives, and a second
    thing to secure, retain and erase.
    """
    llm = get_settings().llm
    identifiers = [node_id for node_id in node_ids if node_id]
    get_ledger().append(
        "egress",
        actor,
        {
            "purpose": purpose,
            "provider": llm.provider,
            "endpoint": host_of(endpoint_for(llm.provider, llm)),
            "nodeIds": identifiers,
            "nodeCount": len(identifiers),
            "fields": sorted(set(fields)),
        },
        session,
    )


def record_decision(
    action: str,
    arguments: dict[str, Any],
    actor: str,
    outcome: str,
    *,
    reversible: bool,
    detail: str = "",
    session: str = "",
) -> None:
    """A change the agent proposed, and what became of it."""
    get_ledger().append(
        "decision",
        actor,
        {
            "action": action,
            "arguments": arguments,
            "outcome": outcome,
            "reversible": reversible,
            "detail": detail,
        },
        session,
    )
