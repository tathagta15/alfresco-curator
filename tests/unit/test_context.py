"""Resolving what the user is pointing at on screen.

The case that matters most is the refusal: an instruction that says "these"
with nothing selected must not fall back to "everything".
"""

from __future__ import annotations

import pytest

from alfresco_curator.agent.context import (
    PageContext,
    Selected,
    apply,
    describe,
    references_items,
    references_place,
    unresolved,
)

IN_FINANCE = PageContext(
    folder_id="f-1",
    folder_name="Finance",
    folder_path="/Company Home/Finance",
)
WITH_SELECTION = PageContext(
    folder_id="f-1",
    folder_name="Finance",
    folder_path="/Company Home/Finance",
    selected=[Selected(id="n-1", name="INV-001.pdf"), Selected(id="n-2", name="INV-002.pdf")],
)


@pytest.mark.parametrize(
    "prompt",
    ["tag these as overdue", "delete those", "archive the selected documents", "rename this file"],
)
def test_pointing_at_items_is_recognised(prompt):
    assert references_items(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    ["what is in this folder", "find invoices here", "tag everything in the current folder"],
)
def test_pointing_at_a_place_is_recognised(prompt):
    assert references_place(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "find the Q3 invoices",
        "why is the architecture document missing",
        "create a folder called Reports",
        # Pronouns that refer back to the conversation, not the page. The
        # thread already carries those, so they must not demand a selection.
        "tag them as overdue",
        "delete it",
    ],
)
def test_ordinary_requests_point_at_nothing(prompt):
    assert references_items(prompt) is False
    assert references_place(prompt) is False


class TestRefusal:
    def test_pointing_at_items_with_nothing_selected_is_refused(self):
        problem = unresolved("tag these as overdue", IN_FINANCE)
        assert "nothing is selected" in problem

    def test_the_refusal_says_how_to_proceed(self):
        assert "Select" in unresolved("delete those", PageContext())

    def test_pointing_at_a_place_with_no_folder_is_refused(self):
        assert unresolved("what is in this folder", PageContext())

    def test_a_resolvable_reference_is_not_refused(self):
        assert unresolved("tag these as overdue", WITH_SELECTION) == ""
        assert unresolved("what is in this folder", IN_FINANCE) == ""

    def test_a_request_pointing_at_nothing_is_never_refused(self):
        assert unresolved("find the Q3 invoices", PageContext()) == ""

    def test_a_question_about_those_is_left_to_the_thread(self):
        # "of those" nearly always means the results just discussed, so
        # refusing it answers a safe question with a dead end.
        assert unresolved("of those, which one is older?", PageContext(), writes=False) == ""

    def test_an_instruction_about_those_is_still_refused(self):
        assert "nothing is selected" in unresolved("delete those", PageContext(), writes=True)

    def test_a_place_has_no_conversational_antecedent(self):
        # A thread never establishes which folder is open, so this stands
        # even for a question.
        assert unresolved("what is in this folder", PageContext(), writes=False)


class TestGrounding:
    def test_the_page_facts_reach_the_prompt(self):
        grounded = apply("tag these as overdue", WITH_SELECTION)
        assert "INV-001.pdf" in grounded and "n-2" in grounded
        assert "tag these as overdue" in grounded

    def test_an_unrelated_request_is_left_alone(self):
        """Attaching context to everything would bias a search towards
        whichever folder happened to be open."""
        assert apply("find the Q3 invoices", WITH_SELECTION) == "find the Q3 invoices"

    def test_nothing_is_attached_when_the_page_knows_nothing(self):
        assert apply("tag these as overdue", PageContext()) == "tag these as overdue"

    def test_the_folder_reaches_a_place_reference(self):
        assert "/Company Home/Finance" in apply("find invoices here", IN_FINANCE)

    def test_a_long_selection_is_capped(self):
        many = PageContext(selected=[Selected(id=f"n-{i}", name=f"f{i}.pdf") for i in range(40)])
        text = describe(many)
        assert "and 15 more" in text
        assert "f30.pdf" not in text
