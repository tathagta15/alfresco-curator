"""Reading the governance record.

An audit trail nobody can read is a claim, not a control. These endpoints let
a compliance officer answer the three questions they are actually asked: what
was instructed, what left the network, and has the record been altered.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from alfresco_curator import governance
from alfresco_curator.config import get_settings

router = APIRouter(prefix="/api/governance", tags=["governance"])


@router.get("/posture")
def posture() -> dict:
    """The controls currently in force, stated plainly enough to screenshot."""
    settings = get_settings()
    ledger = governance.get_ledger()
    allowed = settings.governance.allowed_model_hosts
    return {
        "ledger": {
            "enabled": ledger.enabled,
            # Reported separately: a ledger that is configured but unwritable
            # is the one failure mode that looks like success.
            "writable": ledger.writable(),
            "path": str(ledger.path) if ledger.path else None,
            "records": len(ledger.read()),
        },
        "modelEndpoint": {
            "provider": settings.llm.provider,
            "host": governance.host_of(governance.endpoint_for(settings.llm.provider)),
            "allowlist": allowed,
            "restricted": bool(allowed),
        },
        "dataMinimisation": {
            "enabled": settings.governance.minimise_metadata,
            "withheldFields": list(governance.SENSITIVE_FIELDS)
            if settings.governance.minimise_metadata
            else [],
        },
        # Stated because it is the question every review asks first, and the
        # answer is a property of the code rather than a policy promise.
        "documentContentLeavesTheRepository": False,
        "tracingToThirdParty": settings.llm.tracing_enabled,
    }


@router.get("/ledger")
def ledger(limit: int = Query(default=50, ge=1, le=1000)) -> dict:
    return {"records": governance.get_ledger().tail(limit)}


@router.get("/verify")
def verify() -> dict:
    """Recompute the hash chain and name the first line that does not hold."""
    return governance.get_ledger().verify()
