"""The routing decision, which has to hold without a model in the loop."""

from __future__ import annotations

import pytest

from alfresco_curator.agent.intent import classify


@pytest.mark.parametrize(
    "prompt",
    [
        "find the Q3 invoices",
        "show me everything in Finance",
        "why can't I find the architecture document",
        "which invoices are overdue",
        "how many contracts expired last year",
        "list the files modified this week",
        "I need to find the retention policy",
        "what happened to the budget spreadsheet",
        # Participles describe; they do not instruct.
        "show me the deleted invoices",
        "list everything tagged overdue",
        "find documents created last week",
        "who deleted the vendor contract",
        # A question about an action is still a question.
        "how do I create a folder",
        "why was the invoice moved to Archive",
        # An infinitive is not an instruction: the subject of "to create" here
        # is the explanation, not the agent.
        "show me how to create a folder",
        "what should I delete",
        # A verb that is also a noun must not fire on the noun.
        "find the invoice and file names",
        "the invoice is not showing up in search",
    ],
)
def test_reads_route_to_search(prompt):
    assert classify(prompt).route == "search"
    assert classify(prompt).writes is False


@pytest.mark.parametrize(
    "prompt",
    [
        "create a folder called Q3 Reports",
        "delete the duplicate invoices",
        "tag the overdue invoices",
        "move the contract to Archive",
        "rename the file to final.docx",
        "add a comment to the policy",
        "set the title on every invoice",
        # Politeness must not disguise an instruction.
        "please delete the duplicates",
        "can you create a folder for Q3",
        "hey curator, archive the 2024 contracts",
        "could you move these to Finance",
        # The compound ask: read first, then act.
        "find every overdue invoice and tag it urgent",
        "search for expired contracts and move them to Archive",
        # Wanting a thing that does not exist yet.
        "I need a folder for the Q3 audit",
        # A stated intention, which the bare infinitive case above must not catch.
        "I want to delete the duplicate invoices",
        "I need you to archive the 2024 contracts",
        "clean up the Archive folder",
        "empty the trash",
    ],
)
def test_instructions_route_to_act(prompt):
    intent = classify(prompt)
    assert intent.route == "act"
    assert intent.writes is True


@pytest.mark.parametrize(
    "prompt",
    ["undo that", "undo", "put it back", "roll back that change", "undo everything"],
)
def test_undo_is_its_own_route(prompt):
    assert classify(prompt).route == "undo"


@pytest.mark.parametrize(
    "prompt",
    [
        "revert the contract to version 2",
        "restore the deleted invoice",
    ],
)
def test_repository_reversals_are_actions_not_session_undo(prompt):
    """`revert_version` acts on the repository; undo acts on this session."""
    assert classify(prompt).route == "act"


@pytest.mark.parametrize(
    "prompt",
    [
        "what can you do",
        "which actions are available",
        "is the indexing lagging",
        "who can see the salary review",
        "what permissions are on this folder",
    ],
)
def test_open_questions_route_to_the_deep_agent(prompt):
    intent = classify(prompt)
    assert intent.route == "explore"
    assert intent.writes is False


def test_empty_input_is_total():
    assert classify("").route == "search"
    assert classify("   ").route == "search"


def test_every_route_explains_itself():
    """A system that decides for the user must be able to say why."""
    for prompt in ["find invoices", "delete invoices", "undo that", "what can you do"]:
        assert classify(prompt).reason


def test_only_the_action_route_carries_write_permission():
    """The safety property the whole design rests on.

    `writes` decides whether the write tools are loaded at all, so if any read
    route ever set it, a question could reach an agent holding `delete_node`.
    """
    for prompt in ["find invoices", "why is it missing", "what can you do", "undo that"]:
        intent = classify(prompt)
        assert intent.writes is False, prompt
        assert intent.route != "act", prompt
