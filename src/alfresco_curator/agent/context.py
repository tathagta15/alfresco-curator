"""Resolving "this", "these" and "here" against what the user is looking at.

The repository is asked which query language it speaks rather than the user.
The sentence is read to decide whether it reads or writes, rather than making
the user arm a mode. This is the third application of the same principle: a
user standing in a folder with two documents selected should not have to type
their names back to a system that can already see them.

That makes a deictic phrase - "tag **these** as overdue" - a reference to be
resolved, not an ambiguity to be guessed at. The important case is the one
where it *cannot* be resolved: an instruction that says "these" with nothing
selected must stop and ask, because the plausible fallbacks are "do nothing"
and "do it to everything in the repository", and one of those is a disaster.

Detection is lexical, for the same reason routing is: it gates whether a
destructive instruction runs at all, so it has to hold without a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Selected:
    """A selected item. Share reports a node id; ADW's DOM exposes only a name,
    which the agent can still resolve with its own search tools."""

    id: str = ""
    name: str = ""


@dataclass(frozen=True)
class PageContext:
    """What the host page can see, as reported by the launcher."""

    folder_id: str = ""
    folder_name: str = ""
    folder_path: str = ""
    selected: list[Selected] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.folder_id or self.folder_path or self.selected)


# Phrases that point at the page rather than naming anything. "them" and "it"
# are deliberately absent: they usually refer back to something earlier in the
# conversation, which the thread already carries.
#
# "this folder" belongs to the place pattern below, not here. It resolves
# against the folder the user has open, so treating it as an item reference
# demanded a selection that the phrase never implied.
_ITEM_DEIXIS = re.compile(
    r"\b(?:these|those|this (?:one|document|file|node|item)s?"
    r"|the selected|selected (?:items?|documents?|files?)"
    r"|the current (?:document|file|node))\b"
)
_PLACE_DEIXIS = re.compile(
    r"\b(?:here|this folder|this (?:site|library|space)|the current folder"
    r"|in this folder|where i am)\b"
)


def references_items(prompt: str) -> bool:
    return bool(_ITEM_DEIXIS.search((prompt or "").lower()))


def references_place(prompt: str) -> bool:
    return bool(_PLACE_DEIXIS.search((prompt or "").lower()))


def describe(context: PageContext) -> str:
    """The facts the page can supply, stated plainly for the model.

    Written as observed fact rather than instruction: the agent still decides
    what to do, it simply stops having to ask where it is.
    """
    if context.empty:
        return ""

    parts = []
    where = context.folder_path or context.folder_name
    if where:
        parts.append(f'the user is viewing the folder "{where}"')
        if context.folder_id:
            parts.append(f"whose node id is {context.folder_id}")
    if context.selected:
        listed = ", ".join(_name(item) for item in context.selected[:25])
        more = "" if len(context.selected) <= 25 else f", and {len(context.selected) - 25} more"
        parts.append(f"selected right now: {listed}{more}")
    return "On screen: " + "; ".join(parts) + "."


def _name(item: Selected) -> str:
    label = item.name or item.id or "unnamed"
    return f"{label} (id {item.id})" if item.id and item.name else label


def unresolved(prompt: str, context: PageContext) -> str:
    """Why this request cannot be carried out from the page, if it cannot.

    Returns an empty string when there is nothing in the way.
    """
    if references_items(prompt) and not context.selected:
        return (
            "You have pointed at something on screen, but nothing is selected. "
            "Select the items you mean and ask again, or name them."
        )
    if references_place(prompt) and not (context.folder_id or context.folder_path):
        return (
            "You have asked about where you are, but the page has not told me "
            "which folder that is. Open a folder and ask again, or name it."
        )
    return ""


def apply(prompt: str, context: PageContext) -> str:
    """Prefix the request with the page facts, when they are relevant.

    The context is only attached when the request actually points at the page.
    Attaching it to every request would bias an unrelated search towards
    whichever folder happened to be open.
    """
    if context.empty or not (references_items(prompt) or references_place(prompt)):
        return prompt
    preamble = describe(context)
    return f"{preamble}\n\n{prompt}" if preamble else prompt
