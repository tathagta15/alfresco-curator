"""Show how requests are routed, with no repository and no model involved.

Routing decides which engine runs and whether the write tools are loaded at
all, so it is worth being able to inspect the decision directly rather than
inferring it from an answer.

    python scripts/dev/try_intent.py
    python scripts/dev/try_intent.py "archive last year's contracts"
"""

from __future__ import annotations

import sys

from alfresco_curator.agent.intent import classify

SAMPLES = [
    # Reads: the deterministic ladder answers these.
    "unpaid invoices",
    "why can't I find the architecture document",
    "show me the deleted invoices",
    "how do I create a folder",
    "show me how to create a folder",
    # Open questions: the deep agent, reading only.
    "what can you do to this repository?",
    "is the indexing lagging",
    "who can see the salary review",
    # Instructions: the deep agent, every change held for approval.
    "create a folder 'Q4 Reports' in Shared",
    "tag every UNPAID invoice in Finance as 'overdue'",
    "please delete the duplicates",
    "find every overdue invoice and tag it urgent",
    "I need a folder for the Q3 audit",
    # Putting it back.
    "undo that",
]


def main(argv: list[str]) -> int:
    prompts = argv[1:] or SAMPLES
    width = max(len(p) for p in prompts)
    for prompt in prompts:
        intent = classify(prompt)
        writes = "writes" if intent.writes else "read-only"
        print(f"{prompt:<{width}}  ->  {intent.route:<8} {writes:<9}  {intent.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
