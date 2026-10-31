"""Reading the governance record.

An audit trail nobody can read is a claim, not a control. These endpoints let
a compliance officer answer the three questions they are actually asked: what
was instructed, what left the network, and has the record been altered.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from alfresco_curator import governance
from alfresco_curator.config import get_settings
from alfresco_curator.web.dependencies import CurrentSession

router = APIRouter(prefix="/api/governance", tags=["governance"])


class PolicyUpdate(BaseModel):
    allowed_model_hosts: list[str] = Field(max_length=50)
    minimise_metadata: bool
    trace_to_third_party: bool | None = None
    mutation_budget: int | None = Field(default=None, ge=1, le=1000)
    allow_repository_changes: bool | None = None
    allow_content_changes: bool | None = None
    allow_access_changes: bool | None = None
    allow_folder_rules: bool | None = None
    allow_repository_actions: bool | None = None

    @field_validator("allowed_model_hosts")
    @classmethod
    def validate_hosts(cls, hosts: list[str]) -> list[str]:
        return governance.normalise_hosts(hosts)


def _can_manage(session: Any) -> bool:
    client = session.clients.get("search")
    if client is None:
        return False
    configured = set(get_settings().governance.admin_groups)
    try:
        return bool(configured.intersection(client.get_person_groups()))
    except Exception:
        return False


def _require_manager(session: Any) -> str:
    client = session.clients.get("search")
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Search session required"
        )
    if not _can_manage(session):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Governance changes require membership in an authorized Alfresco group.",
        )
    return client.username or "unknown"


def _policy_snapshot(settings=None) -> dict[str, Any]:
    effective = governance.effective_policy(settings)
    return {
        "allowedModelHosts": effective["allowed_model_hosts"],
        "minimiseMetadata": effective["minimise_metadata"],
        "traceToThirdParty": effective["trace_to_third_party"],
        "mutationBudget": effective["mutation_budget"],
        "allowRepositoryChanges": effective["allow_repository_changes"],
        "allowContentChanges": effective["allow_content_changes"],
        "allowAccessChanges": effective["allow_access_changes"],
        "allowFolderRules": effective["allow_folder_rules"],
        "allowRepositoryActions": effective["allow_repository_actions"],
        "overridden": effective["overridden"],
    }


@router.get("/posture")
def posture(session: CurrentSession) -> dict:
    """The controls currently in force, stated plainly enough to screenshot."""
    settings = get_settings()
    ledger = governance.get_ledger()
    effective = governance.effective_policy(settings)
    allowed = effective["allowed_model_hosts"]
    model_host = governance.host_of(governance.endpoint_for(settings.llm.provider))
    write_block_reason = session.audit.write_blocked_reason if session.audit else ""
    return {
        "canManage": _can_manage(session),
        "repositoryWrites": {
            "state": "read-only" if write_block_reason else "not-tested",
            "reason": write_block_reason,
        },
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
            "currentHostAllowed": not allowed or model_host in {host.lower() for host in allowed},
        },
        "dataMinimisation": {
            "enabled": effective["minimise_metadata"],
            "withheldFields": list(governance.SENSITIVE_FIELDS)
            if effective["minimise_metadata"]
            else [],
        },
        "policyOverridden": effective["overridden"],
        # Stated because it is the question every review asks first, and the
        # answer is a property of the code rather than a policy promise.
        "documentContentLeavesTheRepository": False,
        "tracingToThirdParty": effective["trace_to_third_party"],
    }


@router.get("/settings")
def settings(session: CurrentSession) -> dict:
    """Current effective values and whether this identity may change them."""
    config = get_settings()
    return {
        **_policy_snapshot(config),
        "canManage": _can_manage(session),
        "adminGroups": config.governance.admin_groups,
    }


@router.put("/settings")
def update_settings(body: PolicyUpdate, session: CurrentSession) -> dict:
    actor = _require_manager(session)
    ledger = governance.get_ledger()
    if not ledger.writable():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Governance policy cannot change because the audit ledger is not writable.",
        )

    before = _policy_snapshot()
    allow_folder_rules = before["allowFolderRules"]
    if body.allow_folder_rules is not None:
        allow_folder_rules = body.allow_folder_rules
    elif body.allow_repository_actions is False:
        allow_folder_rules = False

    after = {
        "allowedModelHosts": body.allowed_model_hosts,
        "minimiseMetadata": body.minimise_metadata,
        "traceToThirdParty": (
            before["traceToThirdParty"]
            if body.trace_to_third_party is None
            else body.trace_to_third_party
        ),
        "mutationBudget": (
            before["mutationBudget"] if body.mutation_budget is None else body.mutation_budget
        ),
        "allowRepositoryChanges": (
            before["allowRepositoryChanges"]
            if body.allow_repository_changes is None
            else body.allow_repository_changes
        ),
        "allowContentChanges": (
            before["allowContentChanges"]
            if body.allow_content_changes is None
            else body.allow_content_changes
        ),
        "allowAccessChanges": (
            before["allowAccessChanges"]
            if body.allow_access_changes is None
            else body.allow_access_changes
        ),
        "allowFolderRules": allow_folder_rules,
        "allowRepositoryActions": (
            before["allowRepositoryActions"]
            if body.allow_repository_actions is None
            else body.allow_repository_actions
        ),
        "overridden": True,
    }
    governance.record_policy_change(actor, before, after, "requested")
    governance.write_policy_override(
        body.allowed_model_hosts,
        body.minimise_metadata,
        trace_to_third_party=after["traceToThirdParty"],
        mutation_budget=after["mutationBudget"],
        allow_repository_changes=after["allowRepositoryChanges"],
        allow_content_changes=after["allowContentChanges"],
        allow_access_changes=after["allowAccessChanges"],
        allow_folder_rules=after["allowFolderRules"],
        allow_repository_actions=after["allowRepositoryActions"],
    )
    governance.record_policy_change(actor, before, after, "applied")
    return {**after, "canManage": True}


@router.delete("/settings")
def reset_settings(session: CurrentSession) -> dict:
    actor = _require_manager(session)
    ledger = governance.get_ledger()
    if not ledger.writable():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Governance policy cannot change because the audit ledger is not writable.",
        )

    before = _policy_snapshot()
    governance.record_policy_change(actor, before, {"resetToDeploymentDefaults": True}, "requested")
    governance.reset_policy_override()
    after = _policy_snapshot()
    governance.record_policy_change(actor, before, after, "reset")
    return {**after, "canManage": True}


@router.get("/ledger")
def ledger(session: CurrentSession, limit: int = Query(default=50, ge=1, le=1000)) -> dict:
    client = session.clients.get("search")
    actor = client.username if client else ""
    records = [record for record in governance.get_ledger().read() if record.get("actor") == actor]
    return {"records": records[-limit:]}


@router.get("/verify")
def verify(session: CurrentSession) -> dict:
    """Recompute the hash chain and name the first line that does not hold."""
    return governance.get_ledger().verify()
