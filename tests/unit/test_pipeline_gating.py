"""The decision to spend calls on a pipeline trace, and what it implies."""

from __future__ import annotations

import pytest

from alfresco_curator.diagnostics.search_debugger import pipeline_is_implicated


def facts(**overrides: object) -> dict:
    base = {
        "node_visible_to_user": True,
        "query_rejected_by_repository": False,
        "node_is_deleted": False,
        "failing_clauses": [],
    }
    base.update(overrides)
    return base


class TestPipelineGating:
    def test_healthy_metadata_mismatch_does_not_trace(self):
        """A property mismatch is a content fault; telemetry cannot explain it."""
        assert not pipeline_is_implicated(facts(failing_clauses=['cm:description:"*APPROVED*"']))

    def test_absent_from_index_traces(self):
        assert pipeline_is_implicated(facts(node_visible_to_user=False))

    def test_unknown_visibility_traces(self):
        assert pipeline_is_implicated(facts(node_visible_to_user=None))

    @pytest.mark.parametrize(
        "clause",
        [
            'TEXT:"benchmark"',
            'cm:content.mimetype:"application/pdf"',
            'TEXT:"S3" AND TYPE:"cm:content"',
        ],
    )
    def test_failing_full_text_clause_traces(self, clause):
        """Full text depends on transformation, which the tracer can observe."""
        assert pipeline_is_implicated(facts(failing_clauses=[clause]))

    def test_invalid_query_does_not_trace(self):
        """Every probe erroring is a query fault; the platform is not suspect."""
        assert not pipeline_is_implicated(
            facts(node_visible_to_user=None, query_rejected_by_repository=True)
        )

    def test_deleted_node_does_not_trace(self):
        """A trashcan node is correctly absent; telemetry would only mislead."""
        assert not pipeline_is_implicated(facts(node_visible_to_user=False, node_is_deleted=True))
