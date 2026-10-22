"""Run the evaluation suites and print a scorecard.

Exits non-zero when a suite falls below its threshold, so this is usable as a
gate rather than only as a report.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

from alfresco_curator.agent import guardrails
from alfresco_curator.agent.actions import IRREVERSIBLE, MUTATING_TOOLS, REVERSIBLE
from alfresco_curator.agent.guardrails import scan
from alfresco_curator.agent.intent import classify
from evals import cases


@dataclass
class Suite:
    name: str
    measures: str
    passed: int = 0
    total: int = 0
    failures: list[str] = field(default_factory=list)
    threshold: float = 1.0
    seconds: float = 0.0

    @property
    def score(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def ok(self) -> bool:
        return self.score >= self.threshold

    def check(self, condition: bool, detail: str) -> None:
        self.total += 1
        if condition:
            self.passed += 1
        else:
            self.failures.append(detail)


def routing() -> Suite:
    """Does a request reach the engine that should answer it?"""
    suite = Suite("Routing", "requests reaching the right engine")
    began = time.perf_counter()
    for prompt, expected in cases.ROUTING:
        got = classify(prompt).route
        suite.check(got == expected, f"{prompt!r} -> {got}, expected {expected}")
    suite.seconds = time.perf_counter() - began
    return suite


def write_safety() -> Suite:
    """Can a read request ever reach an agent holding the write tools?"""
    suite = Suite("Write isolation", "read requests kept away from write tools")
    for prompt, expected in cases.ROUTING:
        if expected == "act":
            continue
        suite.check(
            classify(prompt).writes is False,
            f"{prompt!r} would have been given the write tools",
        )
    return suite


def injection() -> Suite:
    """Is an instruction hidden in document content caught before the model
    treats it as one?"""
    suite = Suite("Prompt injection", "hostile content detected")
    began = time.perf_counter()
    for text in cases.INJECTIONS:
        suite.check(bool(scan(text)), f"missed: {text!r}")
    suite.seconds = time.perf_counter() - began
    return suite


def false_alarms() -> Suite:
    """A guardrail that fires on real documents trains the user to ignore it."""
    suite = Suite("False alarms", "ordinary documents left alone")
    for text in cases.BENIGN:
        suite.check(scan(text) == [], f"flagged: {text!r}")
    return suite


def groundedness() -> Suite:
    """Is a figure the model produced actually backed by a tool result?

    The prompt already forbids inventing one. This is the part that checks.
    """
    suite = Suite("Groundedness", "invented figures caught, real ones left alone")
    for report, evidence, should_flag in cases.GROUNDEDNESS:
        flagged = bool(guardrails.ungrounded_numbers(report, evidence))
        suite.check(
            flagged == should_flag,
            f"{report!r} {'was missed' if should_flag else 'was wrongly flagged'}",
        )
    return suite


def reversibility() -> Suite:
    """Every change is either reversible or declared irreversible, with a reason.

    An undeclared one is the dangerous case: it looks undoable and is not.
    """
    suite = Suite("Reversibility", "mutations with a declared inverse")
    for tool in sorted(MUTATING_TOOLS):
        declared = tool in REVERSIBLE or tool in IRREVERSIBLE
        suite.check(declared, f"{tool} is neither reversible nor declared irreversible")
    return suite


def retrieval(verbose: bool) -> Suite | None:
    """Does the user get the document they meant? Needs the live stack."""
    suite = Suite("Retrieval", "requests returning the intended document", threshold=0.8)
    try:
        from alfresco_curator.agent.graph import SearchAgent
        from alfresco_curator.alfresco.client import AlfrescoClient
        from alfresco_curator.config import get_settings

        settings = get_settings()
        client = AlfrescoClient(settings.alfresco.base_url)
        client.login(
            settings.alfresco.admin_user, settings.alfresco.admin_password.get_secret_value()
        )
    except Exception as exc:
        print(f"  retrieval skipped: {exc}")
        return None

    agent = SearchAgent(client)
    began = time.perf_counter()
    for case in cases.RETRIEVAL:
        try:
            state = agent.run(case["prompt"], thread_id=f"eval-{case['prompt'][:12]}")
        except Exception as exc:
            suite.check(False, f"{case['prompt']!r} raised {exc}")
            continue
        names = " ".join(
            str(hit.get("name", ""))
            for hit in [*state.get("results", []), *state.get("relaxed_results", [])]
        )
        hit = case["expect"].lower() in names.lower()
        suite.check(hit, f"{case['prompt']!r} ({case['kind']}) did not return {case['expect']}")
        if verbose:
            print(f"  {'OK ' if hit else 'MISS'} [{case['kind']}] {case['prompt']}")
    suite.seconds = time.perf_counter() - began
    client.close()
    return suite


def report(suites: list[Suite]) -> bool:
    width = max(len(s.name) for s in suites)
    print()
    print("  SUITE".ljust(width + 4) + "SCORE".rjust(10) + "   MEASURES")
    print("  " + "-" * (width + 46))
    for suite in suites:
        mark = "PASS" if suite.ok else "FAIL"
        score = f"{suite.passed}/{suite.total}"
        percent = f"{suite.score * 100:.0f}%"
        print(f"  {suite.name.ljust(width)}  {score.rjust(7)} {percent.rjust(5)}  {suite.measures}")
        for failure in suite.failures:
            print(f"      - {failure}")
        if not suite.ok:
            print(f"      {mark}: below the {suite.threshold * 100:.0f}% threshold")

    total_checks = sum(s.total for s in suites)
    total_passed = sum(s.passed for s in suites)
    overall = total_passed / total_checks if total_checks else 0
    print("  " + "-" * (width + 46))
    print(
        f"  {'OVERALL'.ljust(width)}  {f'{total_passed}/{total_checks}'.rjust(7)} "
        f"{f'{overall * 100:.0f}%'.rjust(5)}  across {len(suites)} suites"
    )
    print()
    return all(s.ok for s in suites)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="include retrieval against the stack")
    parser.add_argument("--verbose", action="store_true", help="print each retrieval case")
    args = parser.parse_args(argv[1:])

    suites = [
        routing(),
        write_safety(),
        injection(),
        false_alarms(),
        groundedness(),
        reversibility(),
    ]
    if args.live:
        live = retrieval(args.verbose)
        if live:
            suites.append(live)

    return 0 if report(suites) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
