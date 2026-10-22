"""A deep agent over the same probes, for open-ended investigation.

The deterministic graph in `graph.py` answers "where is my document" the same
way every time, which is what a diagnostic tool owes its user. This agent
trades that guarantee for reach: it plans, chooses its own probes, and can
delegate to sub-agents, so it can follow a question that does not fit the
fixed ladder.

Both exist on purpose. Use the graph when the answer must be reproducible,
and this when the question is exploratory.
"""

from __future__ import annotations

import logging
from typing import Any

from deepagents import SubAgent, create_deep_agent
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from alfresco_curator import llm
from alfresco_curator.agent.actions import MUTATING_TOOLS, AuditLog, build_action_tools
from alfresco_curator.agent.capabilities import (
    MUTATING_CAPABILITIES,
    build_capability_tools,
)
from alfresco_curator.agent.tools import build_tools
from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.config import get_settings
from alfresco_curator.diagnostics.telemetry import Telemetry

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an Alfresco search investigator. A user cannot find
something, or wants to understand a result. You have live probes against the
repository and its search pipeline.

How to work:
1. Translate the request into a repository query and run `search_repository`.
2. If it returns hits, answer from them. Do not investigate a success.
3. If it returns nothing, find out why before saying anything:
   - `clause_reach` tells you which clause matches nothing on its own.
   - `narrow_conflict` tells you which combination empties a result whose
     clauses all match individually.
   - `path_reach` tells you which folder step stops resolving.
4. Only once the query is cleared of blame, delegate to the `infrastructure`
   sub-agent. Tracing the pipeline costs several round trips, and a healthy
   pipeline is the usual answer.

Rules you must not break:
- Every number you quote must come from a tool result. Never estimate.
- Nothing here proves a document exists. If the pipeline is broken, say that
  a recently uploaded document WOULD not be searchable yet - never say "your
  document exists but is not indexed".
- A zero-hit answer that is faithful beats a widened query that is not.
- Finish with: what you found, why, and the next action.

The framework also gives you a private scratchpad: `ls`, `read_file`,
`write_file`, `edit_file`, `delete`, `glob`, `grep` and `execute`. These touch
a temporary workspace of your own, never Alfresco, and `execute` is not even
wired to a shell here. Use them for notes if you like, but never describe them
to the user and never present them as things you can do to their content -
asked what you can do, answer with the repository tools you hold, not these.
Alfresco content is reached only through the repository tools.
"""

FORENSICS_PROMPT = """You examine why a repository query returned nothing.
Use clause_reach, narrow_conflict and path_reach, then report which specific
clause or combination is responsible, quoting the counts. Do not speculate
about infrastructure - that is someone else's job."""

INFRASTRUCTURE_PROMPT = """You examine the Alfresco search pipeline for one
node. Use trace_pipeline and report the first stage that is unhealthy.

Indexing polls the database; ActiveMQ carries transform and rendition traffic
only, so a broker backlog explains missing full text, not a missing document.
If every stage is healthy, say so plainly - do not manufacture a fault."""


ACTION_RULES = """
You can also change the repository: create folders, rename, move, copy, tag,
set properties and aspects, grant permissions, and delete to the trashcan.
You can also manage versions, locks, the trashcan, share links, comments,
favourites, groups and sites.

When a request does not map to a tool you have, do NOT give up and do not
improvise. Call `list_actions` - the repository publishes everything it can
do, including capabilities this agent was never written for. Then
`describe_action` for its parameters, then `run_action`. That is how you
reach simple workflows, counters, type specialisation, category linking,
image transforms and scripted actions.

How to act:
- Find before you change. Never guess a node id - search or use list_children
  for it, so every change names a node you have actually seen.
- Change one node per call. A loop of explicit calls can be reviewed and
  audited; a single vague action cannot.
- Say what you are about to do and why, before you do it. Each change is held
  for human approval with its arguments visible, and may be rejected.
- If a change is refused, report the refusal and carry on with the rest. You
  act with the signed-in user's permissions, so a refusal is usually a
  permission boundary rather than a bug.
- Repository actions run asynchronously. Verify the effect rather than
  reporting success because the call was accepted.
- Deleting moves a node to the trashcan. Never describe it as permanent.
"""

# A read-only turn holds no write tools, so the model correctly concludes it
# cannot change anything - and then says so when asked what it can do, which
# is false of the product. Routing decides per request: a question gets read
# tools, an instruction gets the write set. The model has to know that, or it
# talks the user out of the capability.
READ_ONLY_TURN = """
This turn is a question, so you hold read tools only and will change nothing.

That is a property of this turn, not a limit of what you can do. When the user
asks you to change something - "tag these as overdue", "create a folder",
"archive this" - that instruction is carried out, with every change shown for
approval first and reversible afterwards. Say so if you are asked what you can
do. Never tell the user that changing the repository is beyond you.
"""


def _subagents(by_name: dict[str, Any]) -> list[SubAgent]:
    return [
        SubAgent(
            name="forensics",
            description=(
                "Work out why a query returned nothing. Give it the query. "
                "Returns which clause or combination is responsible."
            ),
            tools=[by_name[n] for n in ("clause_reach", "narrow_conflict", "path_reach")],
            system_prompt=FORENSICS_PROMPT,
        ),
        SubAgent(
            name="infrastructure",
            description=(
                "Check the indexing pipeline for one node id. Expensive - only "
                "use once the query has been cleared of blame."
            ),
            tools=[by_name["trace_pipeline"]],
            system_prompt=INFRASTRUCTURE_PROMPT,
        ),
    ]


def build_deep_agent(
    client: AlfrescoClient,
    telemetry: Telemetry | None = None,
    checkpointer: MemorySaver | None = None,
    *,
    audit: AuditLog | None = None,
    allow_writes: bool = False,
    use_backup_model: bool = False,
) -> Any:
    """Compile a deep agent bound to one authenticated session.

    The model is a single chat model, not the fallback chain: tool calling
    needs `bind_tools`, which a RunnableWithFallbacks does not expose. The
    caller rebuilds with `use_backup_model` when the primary is exhausted.
    """
    tools = build_tools(client, telemetry)
    by_name = {tool.name: tool for tool in tools}

    settings = get_settings().llm
    if use_backup_model and settings.backup_provider and settings.backup_model:
        model = llm.chat_model(settings.backup_model, provider_name=settings.backup_provider)
    else:
        model = llm.chat_model(llm.model())

    prompt = SYSTEM_PROMPT
    interrupt_on: dict[str, Any] | None = None
    if allow_writes:
        ledger = audit or AuditLog()
        tools = [
            *tools,
            *build_action_tools(client, ledger),
            *build_capability_tools(client, ledger),
        ]
        prompt = f"{SYSTEM_PROMPT}\n{ACTION_RULES}"
        # Only mutations pause for approval. Interrupting reads too would mean
        # a click per probe, and nobody would read the ones that matter.
        interrupt_on = {name: True for name in (*MUTATING_TOOLS, *MUTATING_CAPABILITIES)}
    else:
        prompt = f"{SYSTEM_PROMPT}\n{READ_ONLY_TURN}"

    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=prompt,
        subagents=_subagents(by_name),
        checkpointer=checkpointer or MemorySaver(),
        interrupt_on=interrupt_on,
    )


def run_deep_agent(agent: Any, prompt: str, thread_id: str = "default") -> dict[str, Any]:
    """Invoke the agent and flatten its transcript into a reportable shape."""
    return _collect(
        agent,
        {"messages": [{"role": "user", "content": prompt}]},
        thread_id,
        prompt,
    )


def resume_deep_agent(
    agent: Any, decisions: list[dict[str, Any]], thread_id: str = "default"
) -> dict[str, Any]:
    """Continue a run that paused for approval.

    `decisions` mirrors the pending list, one entry per held call:
    {"type": "approve"} or {"type": "reject", "message": "why"}.
    """
    # The middleware reads a {"decisions": [...]} envelope, not a bare list.
    return _collect(agent, Command(resume={"decisions": decisions}), thread_id, "")


def approve_all(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "approve"} for _ in pending]


def _collect(agent: Any, payload: Any, thread_id: str, prompt: str) -> dict[str, Any]:
    result = agent.invoke(payload, config={"configurable": {"thread_id": thread_id}})

    messages = result.get("messages", [])
    steps: list[dict[str, Any]] = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            steps.append({"node": call["name"], "detail": _summarise(call.get("args", {}))})

    answer = ""
    for message in reversed(messages):
        content = getattr(message, "content", "")
        if (
            isinstance(content, str)
            and content.strip()
            and not getattr(message, "tool_calls", None)
        ):
            answer = content
            break

    return {
        "prompt": prompt,
        "report": answer,
        "steps": steps,
        "todos": result.get("todos", []),
        "messageCount": len(messages),
        "pending": _pending(result),
    }


def _pending(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The changes held for approval, with their arguments visible.

    The interrupt carries `action_requests` plus the decisions each one
    permits, so the caller can offer reject where reject is allowed.
    """
    held: list[dict[str, Any]] = []
    for interrupt in result.get("__interrupt__", []) or []:
        value = getattr(interrupt, "value", None)
        if not isinstance(value, dict):
            continue
        allowed = {
            config.get("action_name"): config.get("allowed_decisions", [])
            for config in value.get("review_configs", []) or []
        }
        for request in value.get("action_requests", []) or []:
            name = request.get("name", "")
            held.append(
                {
                    "action": name,
                    "arguments": request.get("args", {}),
                    "description": request.get("description", ""),
                    "allowedDecisions": allowed.get(name, ["approve", "reject"]),
                }
            )
    return held


def _summarise(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={str(v)[:70]}" for k, v in args.items()) or "no arguments"
