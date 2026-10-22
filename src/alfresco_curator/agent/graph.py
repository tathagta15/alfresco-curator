"""The search agent: find the node, and when it is missing, explain why.

A plain search answers "here are the hits". The question people actually ask
is "where is my document", and that has three very different answers: the
query was wrong, the permissions exclude it, or the indexing pipeline never
made it searchable. Those need different evidence, so the agent gathers the
cheap evidence first and only escalates to infrastructure once the query has
been cleared of blame.

    plan -> search -+-> found
                    |
                    +-> forensics -> recover -+-> query fault  -> postmortem
                                              |
                                              +-> infra sweep  -> postmortem
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from alfresco_curator import llm
from alfresco_curator.agent import semantic
from alfresco_curator.agent.cache import TTLCache
from alfresco_curator.alfresco import afts
from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.diagnostics import tracer
from alfresco_curator.diagnostics.query_generator import generate_query
from alfresco_curator.diagnostics.search_debugger import (
    analyse_conflict,
    analyse_empty_result,
    analyse_path_reach,
)
from alfresco_curator.diagnostics.telemetry import Telemetry
from alfresco_curator.errors import AlfrescoError, LLMError

log = logging.getLogger(__name__)

Cause = Literal["found", "query", "permissions", "infrastructure", "unknown"]

# Every listed hit is attributed, so the page size bounds the probe cost.
MAX_ATTRIBUTED = 10

POSTMORTEM_PROMPT = """You are an Alfresco search incident analyst. A user
searched for something and got nothing back. You are given the query, the
evidence gathered about each clause, and - only when the query was cleared of
blame - the state of the indexing pipeline.

Write a short postmortem in Markdown with exactly these sections:
### What happened
One sentence, in the user's terms, not AFTS jargon.
### Why
The single most likely cause, named precisely, quoting the number that proves
it. If a clause matched nothing, say which. If a combination conflicted, say
which pair. If the pipeline is behind, say which stage and by how much.
### Fix
The concrete next action, as an instruction the user can follow.

Rules:
- Use ONLY the evidence given. Never invent a count, a stage or a document.
- A healthy pipeline means the query is at fault; say so plainly rather than
  hedging towards infrastructure.
- Nothing here proves the document exists. A broken pipeline explains why a
  document WOULD be missing, so write "any document uploaded recently would
  not be searchable yet", never "your document exists but is not indexed".
- If the evidence is genuinely inconclusive, say that and name the one check
  that would settle it.
- Be brief. Three or four sentences per section at most.
"""

ANSWER_PROMPT = """You are an Alfresco search assistant. The search found
documents. Explain what was found and why each one satisfies the request, so
the user can tell at a glance whether it is what they wanted.

Write Markdown with exactly these sections:
### What was found
Name the documents in the user's terms, with what each one actually is -
quote its title, status or location. Do not merely count them.
### Why these matched
Tie the request to the clauses that matched: which part of the query each
document satisfied, and what its metadata says. Quote real values.
### Worth knowing
Anything that would change how the user reads this result - a status that is
not what they asked for, a draft when they wanted approved, more hits than
shown, or a near-duplicate. If there is genuinely nothing, say the result is
unambiguous.

Rules:
- Use ONLY the evidence given. Never invent a title, status, path or count.
- `matchedClauses` lists the clauses each document provably satisfied; use it
  rather than guessing why something matched.
- `documents` covers every result the user can see, so never say that only
  some of them are shown or that others are hidden.
- If totalHits is larger than documentsListed, the remainder are beyond this
  page; say that plainly if it matters.
- Be specific and brief: a few sentences per section.
"""


def _merge_steps(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append within a run, reset between them.

    The checkpointer restores the previous turn's state, so without the reset
    a second question on the same thread would report the first question's
    path as well. Only `run` sends an empty list; nodes with nothing to say
    omit the key entirely.
    """
    return left + right if right else []


class AgentState(TypedDict, total=False):
    """Everything the graph accumulates; nodes add, they never overwrite inputs."""

    prompt: str
    afts_query: str
    explanation: str
    assumptions: list[str]
    warnings: list[str]
    total: int
    results: list[dict[str, Any]]
    clause_counts: list[dict[str, Any]]
    conflict: list[dict[str, Any]]
    path_reach: list[dict[str, Any]]
    relaxed_query: str
    relaxed_total: int
    relaxed_results: list[dict[str, Any]]
    infra: dict[str, Any]
    cause: Cause
    match_evidence: list[dict[str, Any]]
    report: str
    steps: Annotated[list[dict[str, Any]], _merge_steps]
    history: list[dict[str, Any]]
    error: str


def _blank(prompt: str) -> AgentState:
    """A clean turn.

    The checkpointer restores the whole previous turn, so every per-run field
    is cleared explicitly; only `history` is meant to carry over.
    """
    return {
        "prompt": prompt,
        "steps": [],
        "afts_query": "",
        "total": 0,
        "results": [],
        "clause_counts": [],
        "conflict": [],
        "path_reach": [],
        "relaxed_query": "",
        "relaxed_total": 0,
        "relaxed_results": [],
        "infra": {},
        "cause": "unknown",
        "match_evidence": [],
        "report": "",
        "error": "",
        "warnings": [],
    }


def _merge(state: dict[str, Any], update: dict[str, Any]) -> None:
    """Apply one node's output the way the graph's own reducers would."""
    for key, value in update.items():
        if key == "steps":
            state["steps"] = _merge_steps(state.get("steps") or [], value or [])
        else:
            state[key] = value


def _step(name: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"node": name, "detail": detail, **extra}


# Fields that narrow a search without saying what it is about. A query made
# only of these answers "what exists", not "what did I ask for".
_STRUCTURAL = ("TYPE:", "ASPECT:", "PATH:", "SITE:", "PARENT:", "cm:modified:", "cm:created:")


def _says_what(clause: str) -> bool:
    """Does this clause carry the subject of the request?"""
    return not clause.strip().upper().startswith(tuple(f.upper() for f in _STRUCTURAL))


def _hit(entry: dict[str, Any]) -> dict[str, Any]:
    """The same shape the search panel renders, so a find looks like a find."""
    properties = entry.get("properties") or {}
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "nodeType": entry.get("nodeType"),
        "path": (entry.get("path") or {}).get("name"),
        "title": properties.get("cm:title"),
        "description": properties.get("cm:description"),
        "modifiedAt": entry.get("modifiedAt"),
        "mimeType": (entry.get("content") or {}).get("mimeType"),
    }


class SearchAgent:
    """Holds the compiled graph plus the caches that make a run cheap."""

    def __init__(self, client: AlfrescoClient, cache_ttl: float = 30.0) -> None:
        self.client = client
        self.cache = TTLCache(ttl_seconds=cache_ttl)
        self.checkpointer = MemorySaver()
        self.graph = self._build().compile(checkpointer=self.checkpointer)

    # ------------------------------------------------------------- cached probes
    def _count(self, query: str) -> int | None:
        def probe() -> int | None:
            try:
                return self.client.count_matches(query).value
            except AlfrescoError:
                return None

        return self.cache.get_or_call(f"count::{self.client.username}::{query}", probe)

    def _search(self, query: str, max_items: int = 10) -> dict[str, Any]:
        """Deliberately uncached: this is the answer, not evidence.

        Re-asking the same question is how someone checks whether a document
        has finished indexing, and a cached hit would answer with the state of
        the world a minute ago.
        """
        return self.client.search_afts(query, max_items=max_items)

    # -------------------------------------------------------------------- nodes
    def plan(self, state: AgentState) -> AgentState:
        try:
            info = generate_query(state["prompt"], language=self.client.detect_query_language())
        except LLMError as exc:
            return {
                "error": str(exc),
                "cause": "unknown",
                "steps": [_step("plan", f"could not translate the request: {exc}")],
            }
        return {
            "afts_query": info["afts_query"],
            "explanation": info.get("explanation", ""),
            "assumptions": info.get("assumptions", []),
            "warnings": info.get("warnings", []),
            "steps": [_step("plan", "translated the request to AFTS", query=info["afts_query"])],
        }

    def search(self, state: AgentState) -> AgentState:
        query = state.get("afts_query", "")
        if not query:
            return {"total": 0, "results": []}
        try:
            payload = self._search(query)
        except AlfrescoError as exc:
            return {
                "total": 0,
                "results": [],
                "error": str(exc),
                "steps": [_step("search", f"the repository rejected the query: {exc}")],
            }

        listing = payload.get("list", {})
        entries = listing.get("entries", [])
        total = int(listing.get("pagination", {}).get("totalItems", 0))
        results = [_hit(item["entry"]) for item in entries]
        return {
            "total": total,
            "results": results,
            "steps": [_step("search", f"executed the query, {total} hit(s)", hits=total)],
        }

    def forensics(self, state: AgentState) -> AgentState:
        """Cheap, query-only evidence: what each clause and path reaches alone."""
        query = state["afts_query"]
        clause_counts = analyse_empty_result(self.client, query)
        path_reach = analyse_path_reach(self.client, query)
        conflict: list[dict[str, Any]] = []
        if clause_counts and all(row["hits"] for row in clause_counts):
            conflict = analyse_conflict(self.client, query)

        dead = [row["clause"] for row in clause_counts if row["hits"] == 0]
        dead_path = [row["path"] for row in path_reach if row["hits"] == 0]
        detail = (
            f"{len(dead)} clause(s) match nothing on their own"
            if dead
            else "every clause matches on its own; narrowed the combination"
        )
        return {
            "clause_counts": clause_counts,
            "conflict": conflict,
            "path_reach": path_reach,
            "steps": [_step("forensics", detail, deadClauses=dead, deadPaths=dead_path)],
        }

    def recover(self, state: AgentState) -> AgentState:
        """Re-run without the clauses that matched nothing.

        This is what turns a diagnosis into an answer: it shows the user the
        documents they would have got, instead of only naming the mistake.
        """
        query = state["afts_query"]
        dead = {row["clause"] for row in state.get("clause_counts", []) if row["hits"] == 0}
        if not dead:
            return {}

        kept = [c for c in afts.split_top_level_and(query) if c not in dead]
        if not kept or len(kept) == len(afts.split_top_level_and(query)):
            # Nothing survives: every word the user chose is absent from the
            # index. Dropping clauses cannot help, but the document may exist
            # under the vocabulary its author used.
            return self._by_concept(state)

        if not any(_says_what(clause) for clause in kept):
            # Only the scaffolding survived - a type, a date, a path. Running
            # that returns whatever happens to sit in the repository, which is
            # noise wearing the shape of an answer: "what went wrong in
            # September" came back with dashboard XML. The subject matters
            # more than the filter, so try the vocabulary instead.
            return self._by_concept(state)

        relaxed = " AND ".join(kept)
        try:
            payload = self._search(relaxed, max_items=5)
        except AlfrescoError:
            return {}

        listing = payload.get("list", {})
        total = int(listing.get("pagination", {}).get("totalItems", 0))
        if not total:
            return self._by_concept(state)

        candidates = [
            {
                "id": item["entry"].get("id"),
                "name": item["entry"].get("name"),
                "path": (item["entry"].get("path") or {}).get("name"),
            }
            for item in listing.get("entries", [])
        ]
        # A relaxed query matches by construction, so rows are not answers.
        # Judged before they are offered; if none survive, the words were the
        # wrong words and the vocabulary is worth trying instead.
        answers = semantic.rank(state.get("prompt") or "", candidates)
        if not answers:
            return self._by_concept(state)

        return {
            "relaxed_query": relaxed,
            "relaxed_total": total,
            "relaxed_results": answers,
            "steps": [
                _step(
                    "recover",
                    f"dropping the dead clause(s) left {total} hit(s), "
                    f"{len(answers)} of which answer the request",
                    query=relaxed,
                )
            ],
        }

    def _by_concept(self, state: AgentState) -> AgentState:
        """Search the vocabulary a matching document would really use.

        The last resort, and only once the lexical query has been shown to be
        hopeless: it costs a model call, and most searches never reach here.
        """
        prompt = state.get("prompt") or ""
        if not prompt:
            return {}
        found = semantic.search(self.client, prompt, language=self.client.detect_query_language())
        if not found["total"]:
            return {}

        # An OR across eight terms is a wide net, and it catches tags and
        # category nodes alongside documents.
        answers = semantic.rank(prompt, found["results"])
        if not answers:
            return {}

        terms = ", ".join(found["terms"])
        return {
            "relaxed_query": found["query"],
            "relaxed_total": found["total"],
            "relaxed_results": answers,
            "steps": [
                _step(
                    "concept",
                    f"no word of the request is in the index; searching {terms} "
                    f"found {len(answers)} that answer it",
                    query=found["query"],
                )
            ],
        }

    def infra(self, state: AgentState) -> AgentState:
        """Pipeline health, with no node to anchor on.

        Reached only when the query itself is sound, which is exactly when a
        missing document is worth blaming on indexing.
        """

        def sweep() -> dict[str, Any]:
            with Telemetry() as telemetry:
                now_ms = time.time() * 1000
                indexer, _ = tracer.trace_indexer(telemetry, now_ms)
                broker = tracer.trace_broker(telemetry)
                stats = telemetry.index_stats()
            stages = [indexer, broker]
            return {
                "stages": [stage.as_dict() for stage in stages],
                "verdict": tracer.summarise(stages),
                "indexedDocuments": stats.get("documents"),
                "indexError": stats.get("error"),
            }

        try:
            result = self.cache.get_or_call("infra::sweep", sweep)
        except Exception as exc:
            log.debug("infrastructure sweep failed", exc_info=True)
            return {
                "infra": {"error": str(exc)},
                "steps": [_step("infra", f"pipeline telemetry unavailable: {exc}")],
            }

        verdict = result.get("verdict", {})
        healthy = verdict.get("status") == "ok"
        return {
            "infra": result,
            "cause": "query" if healthy else "infrastructure",
            "steps": [
                _step(
                    "infra",
                    verdict.get("summary", "pipeline checked"),
                    brokenAt=verdict.get("brokenAt"),
                )
            ],
        }

    def report(self, state: AgentState) -> AgentState:
        if state.get("total", 0) > 0:
            return self._answer(state)

        evidence = {
            "request": state.get("prompt"),
            "aftsQuery": state.get("afts_query"),
            "searchingAs": self.client.username,
            "clauseReach": state.get("clause_counts"),
            "narrowing": state.get("conflict"),
            "pathReach": state.get("path_reach"),
            "relaxedQuery": state.get("relaxed_query"),
            "relaxedHits": state.get("relaxed_total"),
            "pipeline": state.get("infra"),
        }
        try:
            report = self._write(
                POSTMORTEM_PROMPT,
                "\n".join(f"{k}: {v}" for k, v in evidence.items() if v is not None),
            )
        except LLMError as exc:
            report = (
                "### What happened\nThe search returned nothing.\n\n"
                f"### Why\nThe postmortem model was unavailable ({exc}).\n\n"
                "### Fix\nUse the clause evidence above."
            )
        # "unknown" is the starting value, not a conclusion: reaching this point
        # with no stage having set a cause means the query is what failed.
        settled = state.get("cause")
        return {
            "report": report,
            "cause": settled if settled and settled != "unknown" else "query",
            "steps": [_step("report", "wrote the postmortem")],
        }

    def _match_evidence(self, state: AgentState) -> list[dict[str, Any]]:
        """Which clauses each listed hit provably satisfies.

        Turns "it matched" into "it matched because of these clauses", which
        is what separates an answer from a flag. Every listed result is
        covered so the summary cannot claim some are hidden; probes are cached
        and the clause count is capped to bound the cost.
        """
        clauses = afts.split_top_level_and(state.get("afts_query", ""))
        evidence: list[dict[str, Any]] = []
        for hit in state.get("results", [])[:MAX_ATTRIBUTED]:
            node_id = hit.get("id")
            if not node_id:
                continue
            matched = [
                clause for clause in clauses[:6] if self._count(afts.scope_to_node(clause, node_id))
            ]
            evidence.append(
                {
                    "name": hit.get("name"),
                    "title": hit.get("title"),
                    "description": hit.get("description"),
                    "path": hit.get("path"),
                    "matchedClauses": matched,
                }
            )
        return evidence

    def _write(self, system: str, evidence: str) -> str:
        """Compose the report, streaming it when someone is listening.

        `invoke` has no stream writer, so the same node answers in one piece
        for callers that want one - the tests and the REST endpoint - and
        token by token for the SSE endpoint, without two code paths that can
        disagree about what the report says.
        """
        try:
            writer = get_stream_writer()
        except Exception:
            writer = None

        if writer is None:
            return llm.complete(system, evidence, temperature=0.1)

        pieces: list[str] = []
        for chunk in llm.stream_text(system, evidence, temperature=0.1):
            pieces.append(chunk)
            writer({"token": chunk})
        text = "".join(pieces)
        if not text.strip():
            raise LLMError("the model streamed an empty report")
        return text

    def _answer(self, state: AgentState) -> AgentState:
        documents = self._match_evidence(state)
        evidence = {
            "request": state.get("prompt"),
            "aftsQuery": state.get("afts_query"),
            "totalHits": state.get("total"),
            "documentsListed": len(state.get("results", [])),
            "documents": documents,
            "warnings": state.get("warnings"),
        }
        try:
            report = self._write(
                ANSWER_PROMPT,
                "\n".join(f"{k}: {v}" for k, v in evidence.items() if v),
            )
        except LLMError as exc:
            names = ", ".join(d["name"] for d in documents if d.get("name"))
            report = (
                f"### What was found\n{state['total']} matching document(s)"
                + (f": {names}." if names else ".")
                + f"\n\n### Why these matched\nThe query ran cleanly; the summary "
                f"model was unavailable ({exc}).\n\n### Worth knowing\nThe results "
                "below are the repository's own, so they stand without the summary."
            )
        return {
            "cause": "found",
            "match_evidence": documents,
            "report": report,
            "steps": [_step("report", f"explained {len(documents)} of {state['total']} hit(s)")],
        }

    def remember(self, state: AgentState) -> AgentState:
        entry = {
            "prompt": state.get("prompt"),
            "aftsQuery": state.get("afts_query"),
            "total": state.get("total"),
            "cause": state.get("cause"),
        }
        return {"history": [*state.get("history", []), entry]}

    # --------------------------------------------------------------------- graph
    def _found(self, state: AgentState) -> str:
        if state.get("error") and not state.get("afts_query"):
            return "report"
        return "report" if state.get("total", 0) > 0 else "forensics"

    def _needs_infra(self, state: AgentState) -> str:
        """Escalate only when no clause is individually at fault.

        A dead clause is a complete explanation on its own, so checking the
        pipeline would add latency and invite the model to blame something
        that is working.
        """
        dead = [row for row in state.get("clause_counts", []) if row["hits"] == 0]
        dead_path = [row for row in state.get("path_reach", []) if row["hits"] == 0]
        return "report" if (dead or dead_path) else "infra"

    def _build(self) -> StateGraph:
        graph = StateGraph(AgentState)
        graph.add_node("plan", self.plan)
        graph.add_node("search", self.search)
        graph.add_node("forensics", self.forensics)
        graph.add_node("recover", self.recover)
        graph.add_node("infra", self.infra)
        graph.add_node("report", self.report)
        graph.add_node("remember", self.remember)

        graph.add_edge(START, "plan")
        graph.add_edge("plan", "search")
        graph.add_conditional_edges("search", self._found, ["forensics", "report"])
        graph.add_edge("forensics", "recover")
        graph.add_conditional_edges("recover", self._needs_infra, ["infra", "report"])
        graph.add_edge("infra", "report")
        graph.add_edge("report", "remember")
        graph.add_edge("remember", END)
        return graph

    def run(self, prompt: str, thread_id: str = "default") -> dict[str, Any]:
        config = {"configurable": {"thread_id": thread_id}}
        final = self.graph.invoke(_blank(prompt), config=config)
        final["cacheStats"] = self.cache.stats()
        return final

    def stream(self, prompt: str, thread_id: str = "default") -> Iterator[dict[str, Any]]:
        """The same run, reported as it happens.

        Yields `step` as each stage finishes, `token` as the report is
        composed, and finally `result` with the state `run` would have
        returned. The work is identical; only the reporting differs.
        """
        config = {"configurable": {"thread_id": thread_id}}
        state: dict[str, Any] = dict(_blank(prompt))

        for mode, payload in self.graph.stream(
            state, config=config, stream_mode=["updates", "custom"]
        ):
            if mode == "custom":
                if "token" in payload:
                    yield {"type": "token", "text": payload["token"]}
                continue
            # `updates` arrives as {node_name: partial_state}.
            for update in (payload or {}).values():
                if not isinstance(update, dict):
                    continue
                for step in update.get("steps", []) or []:
                    yield {"type": "step", "step": step}
                _merge(state, update)

        state["cacheStats"] = self.cache.stats()
        yield {"type": "result", "state": state}
