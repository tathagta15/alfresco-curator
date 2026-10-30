"""Agentic search endpoints: the deterministic ladder, and the deep agent."""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from alfresco_curator import governance
from alfresco_curator.agent import SearchAgent
from alfresco_curator.agent.actions import AuditLog, undo_run
from alfresco_curator.agent.context import PageContext, Selected, apply, unresolved
from alfresco_curator.agent.deep import build_deep_agent, resume_deep_agent, run_deep_agent
from alfresco_curator.agent.intent import QUESTION_WORDS, READ_VERBS, Intent, classify
from alfresco_curator.agent.suggestions import get_index
from alfresco_curator.config import get_settings
from alfresco_curator.errors import AlfrescoError, SessionExpiredError
from alfresco_curator.governance.policy import effective_policy
from alfresco_curator.web.dependencies import CurrentSession, Searcher, TelemetryDep
from alfresco_curator.web.schemas import (
    AgentRequest,
    AgentResponse,
    AskResponse,
    DeepAgentResponse,
    ResumeRequest,
    SuggestionsRequest,
    UndoResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/suggestions")
def suggestions(body: SuggestionsRequest, client: Searcher) -> dict:
    """Rank natural-language opener chips over documents visible to this user."""
    settings = get_settings().semantic
    if not settings.enabled:
        return {"suggestions": []}

    context = body.context
    documents: list[dict] = []
    verified_selected_names: list[str] = []
    if context and context.folder_id:
        try:
            entries = client.list_children(context.folder_id, max_items=settings.max_documents)
        except AlfrescoError:
            entries = []
    else:
        result = client.search_afts(
            'TYPE:"cm:content"',
            max_items=settings.max_documents,
            includes=["path"],
            fields=["id", "name", "nodeType"],
            language=client.detect_query_language(),
        )
        entries = [item.get("entry", {}) for item in result.get("list", {}).get("entries", [])]

    for entry in entries:
        if entry.get("nodeType") == "cm:content" or entry.get("isFile") is True:
            documents.append(
                {
                    "id": entry.get("id", ""),
                    "name": entry.get("name", ""),
                    "path": (entry.get("path") or {}).get("name", ""),
                }
            )

    if context:
        known_ids = {item["id"] for item in documents}
        for selected in context.selected:
            if not selected.id or selected.id in known_ids:
                continue
            try:
                entry = client.get_node_details(selected.id)
            except AlfrescoError:
                continue
            documents.append(
                {
                    "id": entry.get("id", selected.id),
                    "name": entry.get("name", ""),
                    "path": (entry.get("path") or {}).get("name", ""),
                }
            )
            if entry.get("name"):
                verified_selected_names.append(entry["name"])

    try:
        index = get_index(
            settings.model_name,
            settings.model_cache_dir,
            settings.index_path,
            settings.max_documents,
        )
        ranked_suggestions = index.suggestions(
            client.username or "",
            documents,
            folder_name=context.folder_name if context else "",
            folder_path=context.folder_path if context else "",
            recent_prompt=body.recent.prompt if body.recent else "",
            recent_outcome=body.recent.outcome if body.recent else "",
            recent_files=[item.model_dump() for item in body.recent.files] if body.recent else [],
            recent_actions=body.recent.actions if body.recent else [],
            undo_available=body.recent.undo_available if body.recent else False,
            selected_names=verified_selected_names,
        )
    except Exception as exc:
        log.warning("local suggestion embeddings unavailable: %s", exc)
        ranked_suggestions = []
    return {"suggestions": ranked_suggestions}


_ACTION_CONFIRMATION = re.compile(
    r"^(?:yes|yeah|yep|sure|okay|ok|please do|go ahead|apply that|apply it|"
    r"use that|use it|do that|do it)\b"
)
_ACTION_REFERENCE = re.compile(
    r"\b(?:that|it|one|those|these|above|suggested|option|description|tag|"
    r"title|property|properties|folder)\b"
)
_CLARIFICATION_CUE = re.compile(
    r"\?|\b(?:which|what|provide|specify|choose|would you like|what .* need)\b",
    re.IGNORECASE,
)
_WORDS = re.compile(r"[a-z][a-z'\-]*", re.IGNORECASE)


def _intent_for_thread(prompt: str, session, thread_id: str) -> Intent:
    intent = classify(prompt)
    if thread_id not in session.pending_action_threads:
        return intent
    if intent.route == "undo":
        session.pending_action_threads.discard(thread_id)
        return intent

    text = (prompt or "").strip().lower()
    words = _WORDS.findall(text)
    affirmation = bool(_ACTION_CONFIRMATION.search(text))
    contextual = bool(_ACTION_REFERENCE.search(text))
    independent_read = bool(READ_VERBS.intersection(words))
    new_question = bool(words and words[0] in QUESTION_WORDS and not contextual)

    if intent.route == "search" and independent_read and not affirmation:
        session.pending_action_threads.discard(thread_id)
        return intent
    if intent.route == "explore" and new_question:
        session.pending_action_threads.discard(thread_id)
        return intent
    if affirmation or contextual or (words and len(words) <= 8 and not new_question):
        return Intent(
            "act",
            "continues the pending repository action",
            writes=True,
        )

    session.pending_action_threads.discard(thread_id)
    return intent


def _remember_action_question(session, thread_id: str, response: DeepAgentResponse) -> None:
    if response.pending:
        session.pending_action_threads.discard(thread_id)
        return
    if response.report and _CLARIFICATION_CUE.search(response.report):
        session.pending_action_threads.add(thread_id)
    else:
        session.pending_action_threads.discard(thread_id)


@router.post("/ask", response_model=AskResponse)
def ask(
    body: AgentRequest,
    session: CurrentSession,
    client: Searcher,
    telemetry: TelemetryDep,
) -> AskResponse:
    """One box. The request decides which engine answers it.

    Whether a sentence is a question or an instruction is a property of the
    sentence, so making the user arm a mode beforehand asked them to classify
    their own request before they had an answer. Routing is lexical and
    therefore reproducible, and the decision is returned so the UI can show
    what was chosen rather than hide it.
    """
    intent = _intent_for_thread(body.prompt, session, body.thread_id)

    # Deixis is resolved before routing acts on it. An instruction that points
    # at the screen with nothing on it must stop here: the alternatives are to
    # do nothing silently, or to apply it to everything, and the second is how
    # an agent destroys a repository. A question is left to the thread.
    page = _page_context(body.context)
    blocked = unresolved(body.prompt, page, writes=intent.writes)
    # Recorded before the refusal branch: an instruction that was stopped is
    # exactly the kind an auditor comes looking for.
    governance.record_ask(
        body.prompt, "clarify" if blocked else intent.route, client.username or "unknown"
    )
    if blocked:
        return AskResponse(
            route="clarify",
            reason="the request points at something not on screen",
            message=blocked,
        )

    grounded = body.model_copy(update={"prompt": apply(body.prompt, page)})
    if intent.route == "undo":
        return AskResponse(
            route=intent.route, reason=intent.reason, undo=undo_changes(session, client)
        )

    if intent.route == "search":
        return AskResponse(
            route=intent.route, reason=intent.reason, search=run_agent(grounded, session, client)
        )

    # Both remaining routes run the deep agent; only one of them is handed the
    # write tools, and even then every change is held for approval.
    deep = run_deep(
        grounded.model_copy(update={"allow_writes": intent.writes}), session, client, telemetry
    )
    return AskResponse(route=intent.route, reason=intent.reason, deep=deep)


def _page_context(incoming) -> PageContext:
    if incoming is None:
        return PageContext()
    return PageContext(
        folder_id=incoming.folder_id,
        folder_name=incoming.folder_name,
        folder_path=incoming.folder_path,
        selected=[Selected(id=item.id, name=item.name) for item in incoming.selected],
    )


@router.post("/ask/stream", include_in_schema=False)
def ask_stream(
    body: AgentRequest,
    session: CurrentSession,
    client: Searcher,
    telemetry: TelemetryDep,
) -> StreamingResponse:
    """The same decision and the same work, reported as it happens.

    The route is known the moment the request is read, long before any search
    runs, so it is sent first: the panel can say what it is about to do
    instead of showing a spinner that means nothing.

    Only the deterministic ladder streams its working. The deep agent plans
    its own path, so there is no stage list to promise in advance; it sends
    its route and then its result.
    """
    intent = _intent_for_thread(body.prompt, session, body.thread_id)
    page = _page_context(body.context)
    blocked = unresolved(body.prompt, page, writes=intent.writes)
    grounded = body.model_copy(update={"prompt": apply(body.prompt, page)})
    governance.record_ask(
        body.prompt, "clarify" if blocked else intent.route, client.username or "unknown"
    )

    def events():
        yield _sse(
            "route",
            {
                "route": "clarify" if blocked else intent.route,
                "reason": "the request points at something not on screen"
                if blocked
                else intent.reason,
            },
        )
        if blocked:
            yield _sse("clarify", {"message": blocked})
            yield _sse("done", {})
            return

        try:
            if intent.route == "undo":
                yield _sse("undo", undo_changes(session, client).model_dump(by_alias=True))
            elif intent.route == "search":
                if session.agent is None or session.agent.client is not client:
                    session.agent = SearchAgent(client)
                for event in session.agent.stream(grounded.prompt, thread_id=body.thread_id):
                    if event["type"] == "result":
                        yield _sse(
                            "search",
                            _agent_response(body.prompt, event["state"]).model_dump(by_alias=True),
                        )
                    else:
                        yield _sse(event["type"], event)
            else:
                deep = run_deep(
                    grounded.model_copy(update={"allow_writes": intent.writes}),
                    session,
                    client,
                    telemetry,
                )
                yield _sse("deep", deep.model_dump(by_alias=True))
        except SessionExpiredError:
            yield _sse("expired", {"detail": "the repository session expired"})
        except Exception as exc:
            log.exception("streamed run failed")
            yield _sse("failed", {"detail": str(exc)})
        yield _sse("done", {})

    # Buffering would hold every event until the run finished, which is the
    # one thing this endpoint exists to avoid.
    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


@router.post("", response_model=AgentResponse)
def run_agent(body: AgentRequest, session: CurrentSession, client: Searcher) -> AgentResponse:
    if session.agent is None or session.agent.client is not client:
        session.agent = SearchAgent(client)

    try:
        state = session.agent.run(body.prompt, thread_id=body.thread_id)
    except SessionExpiredError:
        raise  # handled globally as a 401 so the UI can re-authenticate
    except Exception as exc:
        log.exception("agent run failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return _agent_response(body.prompt, state)


def _agent_response(prompt: str, state: dict) -> AgentResponse:
    """One definition of the search payload, shared by both endpoints."""
    return AgentResponse(
        prompt=prompt,
        aftsQuery=state.get("afts_query", ""),
        explanation=state.get("explanation", ""),
        warnings=state.get("warnings", []),
        total=state.get("total", 0),
        results=state.get("results", []),
        cause=state.get("cause", "unknown"),
        report=state.get("report", ""),
        matchEvidence=state.get("match_evidence", []),
        steps=state.get("steps", []),
        clauseCounts=state.get("clause_counts", []),
        conflict=state.get("conflict", []),
        pathReach=state.get("path_reach", []),
        relaxedQuery=state.get("relaxed_query"),
        relaxedTotal=state.get("relaxed_total"),
        relaxedResults=state.get("relaxed_results", []),
        infra=state.get("infra"),
        cacheStats=state.get("cacheStats", {}),
    )


@router.post("/deep", response_model=DeepAgentResponse)
def run_deep(
    body: AgentRequest,
    session: CurrentSession,
    client: Searcher,
    telemetry: TelemetryDep,
) -> DeepAgentResponse:
    """Exploratory mode: the model plans and chooses its own probes.

    Answers here are not reproducible the way `/api/agent` is, so this is a
    deliberate second endpoint rather than a flag on the first.
    """
    policy = effective_policy()
    allow_writes = body.allow_writes and policy["allow_repository_changes"]
    policy_signature = (
        allow_writes,
        policy["allow_access_changes"],
        policy["allow_repository_actions"],
    )
    # The agent is rebuilt when the write setting changes, because the tool set
    # and the approval gate are fixed at compile time - and when the repository
    # client changes, because the tools close over it. Re-authenticating leaves
    # the old client closed, and an agent still holding it fails every call with
    # "the client has been closed" after the approval has already been given.
    if (
        session.deep_agent is None
        or session.deep_writes != allow_writes
        or session.deep_policy_signature != policy_signature
        or session.deep_client is not client
    ):
        # The ledger belongs to the session, not to the agent. Routing now sets
        # allow_writes per request, so a single question between two changes
        # rebuilds the agent - and resetting the ledger here would silently
        # throw away the undo history for changes already made.
        if session.audit is None or session.deep_client is not client:
            session.audit = AuditLog()
        # The chosen model belongs to the session for the same reason. A
        # rebuild that reverted to an exhausted primary failed on the next
        # turn, and the already-used backup flag then blocked the retry.
        session.deep_agent = build_deep_agent(
            client,
            telemetry,
            audit=session.audit,
            allow_writes=allow_writes,
            use_backup_model=session.deep_backup,
        )
        session.deep_writes = allow_writes
        session.deep_policy_signature = policy_signature
        session.deep_client = client

    def run(agent):
        return run_deep_agent(agent, body.prompt, thread_id=body.thread_id)

    audit_start = len(session.audit.entries) if session.audit else 0
    try:
        result = _deep_result(lambda: run(session.deep_agent), session)
        if allow_writes:
            _remember_action_question(session, body.thread_id, result)
        return _with_turn_audit(result, session, audit_start)
    except HTTPException as exc:
        # A daily quota blocks every model on one gateway, so the only useful
        # retry is the backup provider.
        if not _is_quota_error(exc) or session.deep_backup:
            raise
        log.warning("primary model exhausted, rebuilding the agent on the backup")
        session.deep_agent = build_deep_agent(
            client,
            telemetry,
            audit=session.audit,
            allow_writes=allow_writes,
            use_backup_model=True,
        )
        session.deep_backup = True
        result = _deep_result(lambda: run(session.deep_agent), session)
        if allow_writes:
            _remember_action_question(session, body.thread_id, result)
        return _with_turn_audit(result, session, audit_start)


@router.post("/deep/resume", response_model=DeepAgentResponse)
def resume_deep(body: ResumeRequest, session: CurrentSession) -> DeepAgentResponse:
    """Continue a run that paused for approval, with the user's decisions."""
    if session.deep_agent is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="no run is waiting for approval"
        )
    decisions = [decision.model_dump(exclude_none=True) for decision in body.decisions]
    audit_start = len(session.audit.entries) if session.audit else 0
    result = _deep_result(
        lambda: resume_deep_agent(session.deep_agent, decisions, thread_id=body.thread_id),
        session,
    )
    _remember_action_question(session, body.thread_id, result)
    return _with_turn_audit(result, session, audit_start)


def _with_turn_audit(result: DeepAgentResponse, session, start: int) -> DeepAgentResponse:
    entries = session.audit.entries[start:] if session.audit else []
    return result.model_copy(update={"turn_audit": [entry.as_dict() for entry in entries]})


def _deep_result(call, session) -> DeepAgentResponse:
    settings = get_settings()
    provider = settings.llm.provider
    if session.deep_backup and settings.llm.backup_provider:
        provider = settings.llm.backup_provider
    governance.assert_endpoint_allowed(provider, settings)
    try:
        result = call()
    except SessionExpiredError:
        raise  # handled globally as a 401 so the UI can re-authenticate
    except Exception as exc:
        log.exception("deep agent run failed")
        # A model that is throttled, forbidden or overloaded is not an
        # internal fault, and a 500 invites the user to report a bug in the
        # repository instead of trying again.
        if _model_unavailable(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "The language model is unavailable right now. "
                    "Try again in a moment, or configure a different model."
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    audit = session.audit
    return DeepAgentResponse(
        **result,
        audit=audit.as_list() if audit else [],
        changed=audit.changed if audit else 0,
    )


def _error_text(exc: BaseException) -> str:
    """Every message in the explicit cause chain, lowered.

    The vendor error is re-raised as an HTTPException with a readable message,
    which would otherwise hide the very tokens these predicates match on. Only
    `raise ... from` links are followed: an implicit context can carry an
    unrelated failure that happened to be in flight.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = current.__cause__
    return " ".join(parts).lower()


def _is_quota_error(exc: Exception) -> bool:
    """Whether the primary model is unusable for this account, not merely busy.

    A gateway refuses a model it will never serve - an unfunded paid model, a
    revoked key - with 403, where waiting helps no more than it does for a
    daily cap. Both deserve the backup; treating only the cap as recoverable
    turned a switchable failure into a 500.
    """
    text = _error_text(exc)
    return any(
        token in text
        for token in (
            "429",
            "quota",
            "rate_limit",
            "permission_denied",
            "permission_error",
            "insufficient_quota",
            "billing",
            "payment",
            "paid model",
        )
    )


def _model_unavailable(exc: Exception) -> bool:
    """Whether the model, rather than this service, is what failed."""
    text = _error_text(exc)
    return _is_quota_error(exc) or any(
        token in text for token in ("503", "unavailable", "overloaded", "high demand", "timed out")
    )


@router.post("/deep/undo", response_model=UndoResponse)
def undo_changes(session: CurrentSession, client: Searcher) -> UndoResponse:
    """Put this session's changes back, newest first.

    Approval stops the wrong change before it happens; this is the answer for
    the one that got through.
    """
    if session.audit is None or not session.audit.undoable:
        return UndoResponse(reversed=0, failed=[], irreversible=[], audit=[])

    try:
        outcome = undo_run(client, session.audit)
    except SessionExpiredError:
        raise  # handled globally as a 401 so the UI can re-authenticate
    except Exception as exc:
        log.exception("undo failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return UndoResponse(**outcome, audit=session.audit.as_list())
