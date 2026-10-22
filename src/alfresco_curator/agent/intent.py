"""Deciding what a request *is*, before deciding how to answer it.

The repository is asked which query language it speaks rather than the user,
because that is a property of the deployment. Whether a sentence is a question
or an instruction is, in the same way, a property of the sentence — not a mode
the operator should have to arm beforehand. So the route is read off the text
and then *shown*, rather than configured.

The classifier is lexical on purpose. Routing chooses which engine runs and
whether the write tools are even loaded, so it has to be reproducible and
testable with no model in the loop. A classifier that occasionally sent
"delete every invoice" down the read path — or worse, sent "why is this
missing" to an agent holding `delete_node` — would be impossible to reason
about, and impossible to certify for an enterprise.

Matching is on exact word forms, which does more work than it appears to:
"delete the duplicates" is an instruction, "show the deleted duplicates" is a
description, and only the first carries the bare verb. Participles and
gerunds therefore fall through to the read path without needing a rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

Route = Literal["search", "explore", "act", "undo"]


@dataclass(frozen=True)
class Intent:
    """A routing decision, with the evidence that produced it.

    `reason` is surfaced in the UI. A system that silently decides on the
    user's behalf has to be able to say why, or it is merely opaque.
    """

    route: Route
    reason: str
    writes: bool = False


# Bare verb forms for the things the agent can actually carry out. The list is
# deliberately tied to the tool set: routing to the action engine for something
# it cannot do would trade a useless answer for a confusing one.
MUTATION_VERBS = frozenset(
    {
        "create",
        "make",
        "add",
        "delete",
        "remove",
        "purge",
        "empty",
        "clear",
        "clean",
        "discard",
        "rename",
        "retitle",
        "move",
        "relocate",
        "copy",
        "duplicate",
        "put",
        "place",
        "tag",
        "untag",
        "label",
        "classify",
        "categorise",
        "categorize",
        "set",
        "update",
        "change",
        "edit",
        "amend",
        "apply",
        "assign",
        "attach",
        "lock",
        "unlock",
        "check-in",
        "checkin",
        "checkout",
        "restore",
        "revert",
        "rollback",
        "undelete",
        "recover",
        "share",
        "publish",
        "unshare",
        "comment",
        "annotate",
        "favourite",
        "favorite",
        "star",
        "bookmark",
        "invite",
        "grant",
        "revoke",
        "join",
        "upload",
        "import",
        "ingest",
        "archive",
        "declare",
        "retain",
        "start",
        "trigger",
        "execute",
        "organise",
        "organize",
        "tidy",
        "consolidate",
        "merge",
        "replace",
        "convert",
        "transform",
        "rerun",
    }
)
# "file" and "new" were tried here and withdrawn: "find the invoice and file
# names" is not an instruction to file anything. A verb that is also a common
# noun costs more in false actions than it earns in coverage.

# Verbs that ask to be shown something. Their presence is what distinguishes
# "I need to find the policy" from "I need a folder".
READ_VERBS = frozenset(
    {
        "find",
        "search",
        "show",
        "list",
        "get",
        "locate",
        "look",
        "see",
        "display",
        "count",
        "diagnose",
        "explain",
        "trace",
        "compare",
        "review",
        "tell",
        "know",
        "understand",
        "check",
        "inspect",
        "read",
        "summarise",
        "summarize",
        "describe",
        "identify",
        "retrieve",
    }
)

QUESTION_WORDS = frozenset(
    {"what", "why", "where", "who", "whom", "whose", "when", "how", "which", "whether"}
)

# Coordinators after which a bare verb is a second instruction rather than a
# description: "find the invoices **and tag** them".
#
# "to" is deliberately absent. It would catch "I want **to delete** these", but
# it also catches "show me how **to create** a folder", which is a question.
# First-person intent is matched explicitly below instead, where the subject
# makes it unambiguous.
INSTRUCTION_PIVOTS = frozenset({"and", "then", "also", "please", "plus"})

# The deep agent reads as well as writes, so these send a question to it rather
# than to the clause-by-clause ladder, which only knows how to explain a search.
EXPLORE_SIGNALS = (
    re.compile(r"\bwhat (?:can|do) you\b"),
    re.compile(r"\bwhat are you able\b"),
    re.compile(r"\b(?:which|what) (?:actions|capabilities|tools)\b"),
    re.compile(r"\bcapabilit(?:y|ies)\b"),
    re.compile(r"\b(?:elasticsearch|activemq|solr|index(?:ing|er)?|rendition|transform)\b"),
    re.compile(r"\b(?:queue|lag|backlog|health|pipeline)\b"),
    re.compile(r"\b(?:permission|permissions|acl|access rights?)\b"),
    re.compile(r"\bwho can (?:see|read|edit|access)\b"),
    re.compile(r"\bversion history\b"),
)

# Narrow on purpose. "revert version 2 of the contract" is an action against the
# repository; only a reference to *this session's* changes is a session undo.
UNDO_SIGNALS = (
    re.compile(r"^undo\b"),
    re.compile(r"\bundo (?:that|it|this|those|the last|your|everything|all)\b"),
    re.compile(r"\b(?:roll ?back|revert|reverse) (?:that|it|this|those|the last|your)\b"),
    re.compile(r"\bput (?:it|them|that|those) back\b"),
    re.compile(r"\b(?:take|take it) back\b"),
)

# Stripped before the first word is read, so politeness cannot disguise an
# instruction as a question.
# "copilot" stays listed: people address an assistant by whatever they are used
# to calling it, and a stale wake word must not change how a request is routed.
_POLITENESS = re.compile(
    r"^(?:"
    r"hey|hi|hello|ok(?:ay)?|so|right|now|curator|copilot|alfresco"
    r"|please|kindly"
    r"|can you|could you|would you|will you|can we|could we"
    r"|i(?:'d| would) like you to|i need you to|i want you to"
    r"|go ahead and|for me|let's|lets"
    r")\b[\s,]*"
)

_NEED = re.compile(r"\bi (?:need|want|require)\b|\b(?:i'd|i would) like\b")

# "I want to delete these" — the first-person subject is what separates a stated
# intention from the bare infinitive in "how to delete these".
_NEED_TO = re.compile(
    r"\b(?:i (?:need|want|require)|(?:i'd|i would) like)(?: you)? to ([a-z\-]+)\b"
)

_WORD = re.compile(r"[a-z][a-z'\-]*")


def classify(prompt: str) -> Intent:
    """Route one request. Pure, and total: every input gets a route."""
    text = (prompt or "").strip().lower()
    if not text:
        return Intent("search", "nothing to do")

    if any(signal.search(text) for signal in UNDO_SIGNALS):
        return Intent("undo", "asks for this session's changes to be put back")

    core = _strip_politeness(text)
    tokens = _WORD.findall(core)
    if not tokens:
        return Intent("search", "nothing to do")

    # A leading question word settles it: "how do I delete a site" is a question
    # about deleting, not an instruction to delete.
    if tokens[0] in QUESTION_WORDS:
        if _explores(core):
            return Intent("explore", "an open question about the system")
        return Intent("search", "a question, so nothing will be changed")

    if tokens[0] in MUTATION_VERBS:
        return Intent("act", f"opens with '{tokens[0]}', so it is an instruction", writes=True)

    pivot = _instructed_mutation(tokens)
    if pivot:
        return Intent("act", f"'{pivot}' is given as an instruction", writes=True)

    stated = _NEED_TO.search(core)
    if stated and stated.group(1) in MUTATION_VERBS:
        return Intent("act", f"states an intention to {stated.group(1)}", writes=True)

    # "I need a folder for Q3" asks for something to exist; "I need to find the
    # policy" asks to be shown something that already does.
    if _NEED.search(core) and not (READ_VERBS & set(tokens)) and not (QUESTION_WORDS & set(tokens)):
        return Intent("act", "asks for something that does not exist yet", writes=True)

    if _explores(core):
        return Intent("explore", "an open question about the system")

    return Intent("search", "reads as a search, so nothing will be changed")


def _strip_politeness(text: str) -> str:
    """Peel greetings and requests until the real first word is exposed."""
    previous = None
    while previous != text:
        previous = text
        text = _POLITENESS.sub("", text, count=1).lstrip(" ,")
    return text


def _instructed_mutation(tokens: list[str]) -> str | None:
    """The first mutation verb that follows a coordinator.

    This is what catches the compound ask the demo turns on: "find every
    overdue invoice **and tag** it". The pivot requirement is what keeps
    "the folder and its deleted contents" from reading as an instruction.
    """
    for before, word in pairwise(tokens):
        if word in MUTATION_VERBS and before in INSTRUCTION_PIVOTS:
            return word
    return None


def _explores(text: str) -> bool:
    return any(signal.search(text) for signal in EXPLORE_SIGNALS)
