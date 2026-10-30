from __future__ import annotations

import sqlite3
from array import array

from alfresco_curator.agent.suggestions import LocalMetadataIndex
from alfresco_curator.config import SemanticSettings


class FakeEmbedder:
    def __init__(self):
        self.batch_sizes = []

    def embed(self, texts):
        self.batch_sizes.append(len(texts))
        for text in texts:
            lowered = text.lower()
            if "finance" in lowered or "folder" in lowered:
                yield array("f", [1.0, 0.0, 0.0])
            elif "version" in lowered:
                yield array("f", [0.0, 0.0, 1.0])
            else:
                yield array("f", [0.5, 0.5, 0.0])


def make_index(tmp_path):
    settings = SemanticSettings(
        _env_file=None,
        model_name="test-model",
        model_cache_dir=str(tmp_path / "models"),
        index_path=str(tmp_path / "vectors.sqlite3"),
    )
    return LocalMetadataIndex(settings, embedder=FakeEmbedder()), settings.index_path


def test_suggestions_rank_visible_metadata_and_include_search_and_action(tmp_path):
    index, _ = make_index(tmp_path)

    suggestions = index.suggestions(
        "demo.analyst",
        [{"id": "doc-1", "name": "Budget.pdf", "path": "/Finance"}],
        folder_name="Finance",
        folder_path="/Finance",
    )

    assert len(suggestions) == 4
    assert any("Finance" in suggestion for suggestion in suggestions)
    assert any(suggestion.startswith("Create a folder rule") for suggestion in suggestions)
    assert index._embedder.batch_sizes == [1, 1, 6]


def test_suggestions_follow_the_latest_turn_and_offer_undo_when_available(tmp_path):
    index, _ = make_index(tmp_path)
    documents = [{"id": "doc-1", "name": "Budget.pdf", "path": "/Finance"}]

    after_search = index.suggestions(
        "demo.analyst",
        documents,
        recent_prompt="Find approved budgets",
        recent_outcome="found",
        recent_files=[{"name": "Budget.pdf", "path": "/Finance"}],
    )
    after_change = index.suggestions(
        "demo.analyst",
        documents,
        recent_prompt="Tag Budget.pdf as reviewed",
        recent_outcome="action_completed",
        recent_files=[{"name": "Budget.pdf", "path": "/Finance"}],
        recent_actions=["add_tag"],
        undo_available=True,
    )

    assert after_search[0] == "Show the version history for Budget.pdf."
    assert after_change[0] == "Undo that."


def test_suggestions_remain_outcome_aware_without_visible_documents(tmp_path):
    index, _ = make_index(tmp_path)

    suggestions = index.suggestions(
        "demo.analyst",
        [],
        recent_outcome="action_completed",
        undo_available=True,
    )

    assert suggestions[0] == "Undo that."
    assert len(suggestions) == 3


def test_index_is_scoped_per_user_and_prunes_stale_visible_documents(tmp_path):
    index, database = make_index(tmp_path)
    index.suggestions(
        "analyst-a",
        [
            {"id": "shared-1", "name": "Finance.pdf", "path": "/Finance"},
            {"id": "stale-1", "name": "Old.pdf", "path": "/Finance"},
        ],
        folder_name="Finance",
    )
    index.suggestions(
        "analyst-b",
        [{"id": "private-1", "name": "Private.pdf", "path": "/Legal"}],
        folder_name="Legal",
    )
    index.suggestions(
        "analyst-a",
        [{"id": "shared-1", "name": "Finance.pdf", "path": "/Finance"}],
        folder_name="Finance",
    )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT actor_key, node_id FROM suggestion_vectors ORDER BY node_id"
        ).fetchall()

    assert {node_id for _, node_id in rows} == {"shared-1", "private-1"}
    assert len({actor_key for actor_key, _ in rows}) == 2
