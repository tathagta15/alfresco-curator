"""Request and response models.

Responses are typed so the OpenAPI schema describes real shapes rather than
`object`, and so a field rename cannot silently break the UI.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


# --------------------------------------------------------------------- requests


class ConnectRequest(_Model):
    base_url: str = Field(default="", description="Blank uses the configured default.")
    username: str = ""
    password: str = ""
    ticket: str = Field(
        default="",
        description="An existing Alfresco ticket, as supplied by an embedding "
        "Share session. Takes precedence over username/password.",
    )
    role: Literal["search", "inspect"] = "search"

    @model_validator(mode="after")
    def _require_a_credential(self) -> ConnectRequest:
        if not self.ticket.strip() and not (self.username.strip() and self.password):
            raise ValueError("provide either a ticket, or a username and password")
        return self


QueryLanguage = Literal["afts", "lucene"]


class SearchRequest(_Model):
    prompt: str = Field(min_length=1, max_length=2000)
    max_items: int = Field(default=25, ge=1, le=100)


class RawSearchRequest(_Model):
    query: str = Field(min_length=1, max_length=8000)
    max_items: int = Field(default=25, ge=1, le=100)


class DiagnoseRequest(_Model):
    query: str = Field(min_length=1, max_length=8000)
    node: str = Field(min_length=1, max_length=1024)


class TraceRequest(_Model):
    node: str = Field(min_length=1, max_length=1024)
    query: str = ""
    explain: bool = True


class SelectedItem(_Model):
    id: str = Field(default="", max_length=256)
    name: str = Field(default="", max_length=512)


class PageContextIn(_Model):
    """What the host page can see, reported by the embedded launcher."""

    folder_id: str = Field(default="", alias="folderId", max_length=256)
    folder_name: str = Field(default="", alias="folderName", max_length=512)
    folder_path: str = Field(default="", alias="folderPath", max_length=2048)
    # Capped because this arrives from the browser and is pasted into a prompt.
    selected: list[SelectedItem] = Field(default=[], max_length=200)


class AgentRequest(_Model):
    prompt: str = Field(min_length=1, max_length=2000)
    thread_id: str = Field(
        default="default",
        max_length=128,
        description="Groups turns into one conversation so the agent keeps context.",
    )
    allow_writes: bool = Field(
        default=False,
        alias="allowWrites",
        description="Offer the action tools. Every change still needs approval.",
    )
    context: PageContextIn | None = Field(
        default=None,
        description="Where the user is standing, so 'these' and 'here' resolve.",
    )


class SuggestionFile(_Model):
    name: str = Field(default="", max_length=512)
    path: str = Field(default="", max_length=2048)


class SuggestionContextIn(_Model):
    prompt: str = Field(default="", max_length=1200)
    route: Literal["search", "explore", "act", "undo", "clarify"] | None = None
    outcome: Literal[
        "found",
        "query",
        "permissions",
        "infrastructure",
        "action_completed",
        "action_blocked",
        "action_rejected",
        "investigation",
        "unknown",
    ] | None = None
    files: list[SuggestionFile] = Field(default_factory=list, max_length=8)
    actions: list[str] = Field(default_factory=list, max_length=8)
    undo_available: bool = False


class SuggestionsRequest(_Model):
    context: PageContextIn | None = None
    recent: SuggestionContextIn | None = None


class Decision(_Model):
    type: Literal["approve", "reject"] = "approve"
    message: str | None = Field(
        default=None, description="Why it was rejected, fed back to the agent."
    )


class ResumeRequest(_Model):
    thread_id: str = Field(default="default", max_length=128)
    decisions: list[Decision] = Field(
        min_length=1, description="One per held change, in the order they were offered."
    )


# -------------------------------------------------------------------- responses


class LLMInfo(_Model):
    provider: str
    model: str


class HealthResponse(_Model):
    status: str
    llm: LLMInfo
    defaults: dict[str, str]


class StatusResponse(_Model):
    searching_as: str | None = Field(default=None, alias="searchingAs")
    inspecting_as: str | None = Field(default=None, alias="inspectingAs")
    base_url: str | None = Field(default=None, alias="baseUrl")
    auth_source: str | None = Field(default=None, alias="authSource")
    llm: LLMInfo | None = None


class SearchHit(_Model):
    id: str | None = None
    name: str | None = None
    node_type: str | None = Field(default=None, alias="nodeType")
    path: str | None = None
    title: str | None = None
    description: str | None = None
    modified_at: str | None = Field(default=None, alias="modifiedAt")
    mime_type: str | None = Field(default=None, alias="mimeType")


class ClauseCount(_Model):
    clause: str
    hits: int | None
    exact: bool = True


class PathReach(_Model):
    path: str
    hits: int | None


class ConflictStep(_Model):
    clause: str
    hits: int | None
    exact: bool = True


class SearchResponse(_Model):
    afts_query: str = Field(alias="aftsQuery")
    language: QueryLanguage = "afts"
    explanation: str = ""
    assumptions: list[str] = []
    warnings: list[str] = []
    fields_used: list[str] = Field(default=[], alias="fieldsUsed")
    total: int = 0
    total_is_exact: bool = Field(
        default=True,
        alias="totalIsExact",
        description="False when more rows exist beyond the page, making "
        "`total` a lower bound rather than a count.",
    )
    results: list[SearchHit] = []
    clause_counts: list[ClauseCount] = Field(default=[], alias="clauseCounts")
    path_reach: list[PathReach] = Field(
        default=[],
        alias="pathReach",
        description="Ancestors of the query's PATH, deepest first, with hit "
        "counts - shows which folder step stops resolving.",
    )
    conflict: list[ConflictStep] = Field(
        default=[],
        description="Clauses re-added one at a time; the last entry is the "
        "one that emptied the result.",
    )


class AgentStep(_Model):
    node: str
    detail: str


class AgentResponse(_Model):
    prompt: str
    afts_query: str = Field(default="", alias="aftsQuery")
    explanation: str = ""
    warnings: list[str] = []
    total: int = 0
    results: list[dict[str, Any]] = []
    cause: Literal["found", "query", "permissions", "infrastructure", "unknown"] = "unknown"
    report: str = ""
    match_evidence: list[dict[str, Any]] = Field(
        default=[],
        alias="matchEvidence",
        description="Per-document attribution: which clauses each hit satisfied.",
    )
    steps: list[AgentStep] = Field(
        default=[], description="The path the agent actually took through the graph."
    )
    clause_counts: list[ClauseCount] = Field(default=[], alias="clauseCounts")
    conflict: list[ConflictStep] = []
    path_reach: list[PathReach] = Field(default=[], alias="pathReach")
    relaxed_query: str | None = Field(default=None, alias="relaxedQuery")
    relaxed_total: int | None = Field(default=None, alias="relaxedTotal")
    relaxed_results: list[dict[str, Any]] = Field(default=[], alias="relaxedResults")
    infra: dict[str, Any] | None = None
    cache_stats: dict[str, int] = Field(default={}, alias="cacheStats")


class PendingChange(_Model):
    action: str
    arguments: dict[str, Any] = {}
    description: str = ""
    allowed_decisions: list[str] = Field(default=[], alias="allowedDecisions")


class DeepAgentResponse(_Model):
    prompt: str
    report: str = ""
    steps: list[AgentStep] = Field(
        default=[], description="Tool calls the model chose, in the order it made them."
    )
    todos: list[dict[str, Any]] = Field(
        default=[], description="The agent's own plan, if it wrote one."
    )
    message_count: int = Field(default=0, alias="messageCount")
    pending: list[PendingChange] = Field(
        default=[], description="Changes held for approval before anything is written."
    )
    audit: list[dict[str, Any]] = Field(
        default=[], description="Every change attempted this session, including refusals."
    )
    turn_audit: list[dict[str, Any]] = Field(
        default=[], alias="turnAudit", description="Changes attempted during this turn only."
    )
    changed: int = 0


class UndoResponse(_Model):
    reversed: int = 0
    failed: list[dict[str, Any]] = []
    irreversible: list[str] = Field(
        default=[], description="Actions with no inverse, such as run_action."
    )
    audit: list[dict[str, Any]] = []


class AskResponse(_Model):
    """The single-ask envelope: which engine answered, why, and its result.

    Exactly one payload is populated, chosen by `route`. The alternative was a
    flattened union, which would have made every field optional and lost the
    typing that keeps the UI honest.
    """

    route: Literal["search", "explore", "act", "undo", "clarify"]
    reason: str = Field(description="Why this route was chosen, shown to the user.")
    message: str = Field(
        default="",
        description="Set on the clarify route: what the request pointed at that is not there.",
    )
    search: AgentResponse | None = None
    deep: DeepAgentResponse | None = None
    undo: UndoResponse | None = None


class DiagnoseResponse(_Model):
    node_id: str = Field(alias="nodeId")
    facts: dict[str, Any]
    report: str
    node: dict[str, Any]
    parent_acl: dict[str, Any] | None = Field(default=None, alias="parentAcl")
    pipeline: dict[str, Any] | None = Field(
        default=None,
        description="Live indexing pipeline telemetry, present only when the "
        "index rather than the query is implicated.",
    )
    inspected_as: str | None = Field(default=None, alias="inspectedAs")
    searching_as: str | None = Field(default=None, alias="searchingAs")


class TraceStage(_Model):
    stage: str
    status: Literal["ok", "warn", "fail"]
    headline: str
    detail: dict[str, Any] = {}


class TraceVerdict(_Model):
    broken_at: str | None = Field(default=None, alias="brokenAt")
    summary: str
    status: Literal["ok", "warn", "fail"]


class TraceResponse(_Model):
    node_id: str = Field(alias="nodeId")
    searching_as: str | None = Field(default=None, alias="searchingAs")
    stages: list[TraceStage]
    verdict: TraceVerdict
    report: str = ""
    report_error: str | None = Field(default=None, alias="reportError")
