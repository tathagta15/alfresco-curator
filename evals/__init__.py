"""Measured behaviour, not claimed behaviour.

Three of the four suites need no model and no repository, which is what makes
them worth quoting: routing, guardrails and reversibility are properties of the
code, so the numbers are reproducible on any machine and move only when
something actually changes. Retrieval needs a live repository and is reported
separately, because it depends on seeded content.

    python -m evals.run            # everything that needs no services
    python -m evals.run --live     # adds retrieval against the running stack
"""
