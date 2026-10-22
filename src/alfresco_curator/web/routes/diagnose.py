"""Root-cause diagnosis for a single node."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from alfresco_curator.diagnostics.search_debugger import diagnose
from alfresco_curator.errors import (
    AlfrescoError,
    LLMError,
    QueryValidationError,
    SessionExpiredError,
)
from alfresco_curator.web.dependencies import CurrentSession, Searcher, TelemetryDep
from alfresco_curator.web.schemas import DiagnoseRequest, DiagnoseResponse

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["diagnose"])


@router.post("/diagnose", response_model=DiagnoseResponse)
def run_diagnosis(
    body: DiagnoseRequest, client: Searcher, session: CurrentSession, telemetry: TelemetryDep
) -> DiagnoseResponse:
    try:
        outcome = diagnose(
            client,
            body.query,
            body.node,
            session.clients.get("inspect"),
            telemetry=telemetry,
        )
    except (QueryValidationError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SessionExpiredError:
        raise  # handled globally as a 401 so the UI can re-authenticate
    except AlfrescoError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return DiagnoseResponse(
        nodeId=outcome["node_id"],
        facts=outcome["facts"],
        report=outcome["report"],
        node=outcome["node"],
        parentAcl=outcome["parent_acl"],
        pipeline=outcome["pipeline"],
        inspectedAs=outcome["inspected_as"],
        searchingAs=client.username,
    )
