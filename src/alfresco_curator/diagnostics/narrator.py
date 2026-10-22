"""Plain-English narration of a pipeline trace.

The trace itself is produced entirely from observed telemetry; the model is
given those facts and asked only to explain them, in the same spirit as the
search debugger.
"""

from __future__ import annotations

import json
from typing import Any

from alfresco_curator import llm

PIPELINE_SYSTEM_PROMPT = """You are an Alfresco Search Enterprise pipeline
diagnostics engineer. You are given a stage-by-stage trace of one document
through a real deployment. Every value is measured, not inferred.

The architecture is ACS 26.2 with alfresco-elasticsearch-batch-indexing:
- The indexer POLLS THE REPOSITORY DATABASE on a fixed interval. The search
  index is NOT fed from JMS. Never claim an indexing event is "stuck in an
  ActiveMQ queue" - that is a different (live-indexing) architecture.
- ActiveMQ here carries transform and rendition traffic only. A transform
  backlog delays FULL TEXT EXTRACTION, not metadata indexing.
- The indexer writes three modules per node: metadata, path and content. The
  Elasticsearch document carries a separate watermark for each.
- Search results are filtered by the READER / DENIED lists stored in the
  Elasticsearch document, not by a live permission check.

Rules:
- Attribute the failure to the FIRST stage that is broken. Later stages cannot
  succeed if an earlier one failed.
- Quote the actual measured numbers (ages in seconds, queue depths, counts).
- If every stage is healthy, say so plainly and do not invent a problem.
- Distinguish "not yet indexed" (lag) from "indexed but ACL-filtered" from
  "indexed but the query does not match".

Return Markdown with exactly these sections:
### Verdict
One sentence naming the stage at fault, or confirming the pipeline is healthy.
### What the telemetry shows
Bullets walking the stages in order, quoting measured values.
### What to do
Concrete next step: wait N seconds, trigger a reindex, grant a permission,
fix the query, or investigate transform failures.
"""


def narrate(trace: dict[str, Any]) -> str:
    facts = json.dumps(trace, indent=2, default=str)
    return llm.complete(
        PIPELINE_SYSTEM_PROMPT,
        f"Pipeline trace:\n{facts}",
        temperature=0.1,
    )
