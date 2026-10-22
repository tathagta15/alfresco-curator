"""End-to-end indexing pipeline trace."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from alfresco_curator.diagnostics.narrator import narrate
from alfresco_curator.diagnostics.tracer import trace_node
from alfresco_curator.errors import (
    AlfrescoError,
    LLMError,
    QueryValidationError,
    SessionExpiredError,
)
from alfresco_curator.web.dependencies import Searcher, TelemetryDep
from alfresco_curator.web.schemas import TraceRequest, TraceResponse

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["trace"])


@router.post("/trace", response_model=TraceResponse)
def run_trace(body: TraceRequest, client: Searcher, telemetry: TelemetryDep) -> TraceResponse:
    try:
        result = trace_node(client, body.node, telemetry, body.query or None)
    except (QueryValidationError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SessionExpiredError:
        raise  # handled globally as a 401 so the UI can re-authenticate
    except AlfrescoError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    response = TraceResponse(**result)
    if body.explain:
        # A narration failure must not discard a trace that is already complete.
        try:
            response.report = narrate(result)
        except LLMError as exc:
            log.warning("pipeline narration failed: %s", exc)
            response.report_error = str(exc)
    return response
