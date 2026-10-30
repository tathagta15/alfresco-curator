"""Permission-scoped local embeddings for contextual prompt suggestions."""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import threading
from array import array
from functools import lru_cache
from pathlib import Path
from typing import Any

from alfresco_curator.config import SemanticSettings

_MODEL_LOCK = threading.Lock()
_INDEX_LOCK = threading.RLock()

_SEARCH_INTENTS = (
    (
        "Find related documents",
        "Search for documents about topics semantically similar to a known file.",
        lambda name, folder: f"Find documents related to {name}.",
    ),
    (
        "Explain a missing result",
        "Diagnose why a known document might not appear in a repository search.",
        lambda name, folder: f"Why might I not find {name} in search?",
    ),
    (
        "Inspect version history",
        "Read the version history of a repository document without changing it.",
        lambda name, folder: f"Show the version history for {name}.",
    ),
    (
        "Browse this folder",
        "Find and summarize content within the folder the user is browsing.",
        lambda name, folder: (
            f"Find recent documents in {folder}."
            if folder
            else "Find recent documents in this repository."
        ),
    ),
)

_ACTION_INTENTS = (
    (
        "Tag a document",
        "Apply a reversible tag to a document with the user's approval.",
        lambda name, folder: (
            f'Tag "{name}" as reviewed.' if name else "Tag a document as reviewed."
        ),
    ),
    (
        "Create a folder rule",
        "Propose an Alfresco folder rule that applies the versionable aspect to new PDFs.",
        lambda name, folder: (
            f"Create a folder rule for {folder} to add the cm:versionable aspect to new PDFs."
            if folder
            else "Create a folder rule that adds the cm:versionable aspect to new PDFs."
        ),
    ),
)

_FOLLOWUP_SEARCH_INTENTS = {
    "found": (
        (
            "Inspect the result",
            "Read more about a document that was just found.",
            lambda name, folder: f"Show the version history for {name}.",
        ),
        (
            "Find related documents",
            "Find documents related to a result from the previous search.",
            lambda name, folder: f"Find documents related to {name}.",
        ),
    ),
    "query": (
        (
            "Broaden the search",
            "Retry a search after a query returned no accessible matches.",
            lambda name, folder: (
                f"Search for documents related to {name} without limiting to one folder."
            ),
        ),
        (
            "Check another folder",
            "Look for a missing document outside the current folder.",
            lambda name, folder: f"Look for {name} in other folders.",
        ),
    ),
    "permissions": (
        (
            "Check document access",
            "Explain why a result may be excluded by Alfresco permissions.",
            lambda name, folder: f"Check why I may not have access to {name}.",
        ),
    ),
    "infrastructure": (
        (
            "Check indexing status",
            "Inspect the search indexing pipeline after a failed lookup.",
            lambda name, folder: "Check whether the indexing pipeline is healthy.",
        ),
    ),
    "action_completed": (
        (
            "Verify the change",
            "Read back a recent repository action to confirm its effect.",
            lambda name, folder: f"Check the current state of {name}.",
        ),
        (
            "Review the change",
            "Review a completed repository action and what it affected.",
            lambda name, folder: f"Summarize the recent change to {name}.",
        ),
    ),
    "action_blocked": (
        (
            "Check access",
            "Read the permissions that may have blocked the previous action.",
            lambda name, folder: f"Check my access to {name}.",
        ),
    ),
    "action_rejected": (
        (
            "Revise the proposal",
            "Create a revised repository change proposal without applying it.",
            lambda name, folder: f"Suggest a different change for {name}.",
        ),
    ),
}


def _next_step(outcome: str, name: str, folder: str, undo_available: bool) -> str:
    if outcome == "found":
        return (
            f"Show the version history for {name}."
            if name
            else "Show a document's version history."
        )
    if outcome == "query":
        return (
            f"Search for {name} without limiting to one folder."
            if name
            else "Search again without the current folder restriction."
        )
    if outcome == "permissions":
        return f"Who can access {name}?" if name else "Who can access this folder?"
    if outcome == "infrastructure":
        return "Check the search indexing pipeline status."
    if outcome == "action_completed":
        if undo_available:
            return "Undo that."
        return f"Check the current state of {name}." if name else "Review the completed change."
    if outcome == "action_blocked":
        return (
            f"What caused the permission denial for {name}?"
            if name
            else "What caused the permission denial?"
        )
    if outcome == "action_rejected":
        return (
            f"Suggest a safer alternative for {name}."
            if name
            else "Suggest a safer alternative."
        )
    return (
        f"Find recent documents in {folder}."
        if folder
        else "Find recent documents in this repository."
    )


@lru_cache(maxsize=2)
def _embedding_model(model_name: str, cache_dir: str):
    from fastembed import TextEmbedding

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    return TextEmbedding(model_name=model_name, cache_dir=cache_dir)


class LocalMetadataIndex:
    """Store only names and paths, separated by the Alfresco username."""

    def __init__(self, settings: SemanticSettings, *, embedder: Any = None) -> None:
        self.settings = settings
        self._embedder = embedder

    def suggestions(
        self,
        username: str,
        documents: list[dict[str, Any]],
        *,
        folder_name: str = "",
        folder_path: str = "",
        selected_names: list[str] | None = None,
        recent_prompt: str = "",
        recent_outcome: str = "",
        recent_files: list[dict[str, str]] | None = None,
        recent_actions: list[str] | None = None,
        undo_available: bool = False,
    ) -> list[str]:
        rows = self._sync_user_index(username, documents)
        if not rows:
            folder = folder_name or _parent_path(folder_path)
            next_step = _next_step(recent_outcome, "", folder, undo_available)
            browse = (
                f"Find recent documents in {folder}."
                if folder
                else "Find recent documents in this repository."
            )
            rule = (
                f"Create a folder rule for {folder} to add the cm:versionable aspect to new PDFs."
                if folder
                else "Create a folder rule that adds the cm:versionable aspect to new PDFs."
            )
            return list(dict.fromkeys([next_step, browse, rule]))

        context = " ".join(
            part for part in [folder_name, folder_path, *(selected_names or [])] if part.strip()
        )
        prior_files = recent_files or []
        query = " ".join(
            part
            for part in [
                context,
                recent_prompt,
                recent_outcome,
                *(item.get("name", "") for item in prior_files),
                *(item.get("path", "") for item in prior_files),
                *(recent_actions or []),
            ]
            if part.strip()
        ) or "Find useful repository documents and manage them safely."
        query_vector = self._embed([query])[0]
        ranked = sorted(rows, key=lambda row: _cosine(query_vector, row["vector"]), reverse=True)
        visible_by_name = {row["name"].casefold(): row for row in ranked}
        recent_name = next(
            (item.get("name", "") for item in prior_files if item.get("name")), ""
        )
        focus = visible_by_name.get(recent_name.casefold(), ranked[0])
        focus_name = focus["name"]
        folder = folder_name or _parent_path(folder_path or focus["path"])

        candidates = _FOLLOWUP_SEARCH_INTENTS.get(recent_outcome, _SEARCH_INTENTS)
        search_intents = tuple(dict.fromkeys((*candidates, *_SEARCH_INTENTS)))
        all_intents = (*search_intents, *_ACTION_INTENTS)
        intent_vectors = self._embed([intent[1] for intent in all_intents])
        search_count = len(search_intents)
        searches = [
            intent
            for intent, _ in sorted(
                zip(search_intents, intent_vectors[:search_count], strict=True),
                key=lambda item: _cosine(query_vector, item[1]),
                reverse=True,
            )
        ]
        actions = [
            intent
            for intent, _ in sorted(
                zip(_ACTION_INTENTS, intent_vectors[search_count:], strict=True),
                key=lambda item: _cosine(query_vector, item[1]),
                reverse=True,
            )
        ]
        prompts = [_next_step(recent_outcome, focus_name, folder, undo_available)]
        for _, _, make in searches:
            prompt = make(focus_name, folder)
            if prompt not in prompts:
                prompts.append(prompt)
            if len(prompts) == 3:
                break
        prompts.append(actions[0][2](focus_name, folder))
        return list(dict.fromkeys(prompts))

    def _sync_user_index(
        self, username: str, documents: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not username:
            return []
        normalized = []
        for document in documents[: self.settings.max_documents]:
            node_id = str(document.get("id") or "")
            name = " ".join(str(document.get("name") or "").split())
            path = " ".join(str(document.get("path") or "").split())
            if node_id and name:
                text = f"Document: {name}\nFolder: {path}"
                fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
                normalized.append(
                    {
                        "id": node_id,
                        "name": name,
                        "path": path,
                        "text": text,
                        "fingerprint": fingerprint,
                    }
                )
        if not normalized:
            return []

        actor_key = hashlib.sha256(username.casefold().encode("utf-8")).hexdigest()
        database = Path(self.settings.index_path)
        database.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with _INDEX_LOCK, sqlite3.connect(database, timeout=30) as connection:
            os.chmod(database, 0o600)
            connection.execute(
                """CREATE TABLE IF NOT EXISTS suggestion_vectors (
                    actor_key TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    vector BLOB NOT NULL,
                    PRIMARY KEY (actor_key, node_id)
                )"""
            )
            existing = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    "SELECT node_id, fingerprint, vector FROM suggestion_vectors WHERE actor_key=?",
                    (actor_key,),
                )
            }
            changed = [
                document
                for document in normalized
                if existing.get(document["id"], (None, None))[0] != document["fingerprint"]
            ]
            vectors = self._embed([document["text"] for document in changed]) if changed else []
            vector_by_id = {
                document["id"]: vector for document, vector in zip(changed, vectors, strict=True)
            }
            visible_ids = {document["id"] for document in normalized}
            for node_id in existing.keys() - visible_ids:
                connection.execute(
                    "DELETE FROM suggestion_vectors WHERE actor_key=? AND node_id=?",
                    (actor_key, node_id),
                )
            for document in normalized:
                vector = vector_by_id.get(
                    document["id"], existing.get(document["id"], (None, None))[1]
                )
                connection.execute(
                    """INSERT OR REPLACE INTO suggestion_vectors
                       (actor_key, node_id, name, path, fingerprint, vector)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        actor_key,
                        document["id"],
                        document["name"],
                        document["path"],
                        document["fingerprint"],
                        vector,
                    ),
                )
            return [
                {"id": row[0], "name": row[1], "path": row[2], "vector": _unpack(row[3])}
                for row in connection.execute(
                    "SELECT node_id, name, path, vector FROM suggestion_vectors WHERE actor_key=?",
                    (actor_key,),
                )
            ]

    def _embed(self, texts: list[str]) -> list[bytes]:
        if not texts:
            return []
        with _MODEL_LOCK:
            model = self._embedder or _embedding_model(
                self.settings.model_name, self.settings.model_cache_dir
            )
            return [_pack(vector) for vector in model.embed(texts)]


@lru_cache(maxsize=4)
def get_index(model_name: str, cache_dir: str, index_path: str, max_documents: int):
    return LocalMetadataIndex(
        SemanticSettings(
            _env_file=None,
            model_name=model_name,
            model_cache_dir=cache_dir,
            index_path=index_path,
            max_documents=max_documents,
        )
    )


def _pack(vector: Any) -> bytes:
    return array("f", (float(value) for value in vector)).tobytes()


def _unpack(vector: bytes) -> array:
    values = array("f")
    values.frombytes(vector)
    return values


def _cosine(left: Any, right: Any) -> float:
    left_values = _unpack(left) if isinstance(left, bytes) else left
    right_values = _unpack(right) if isinstance(right, bytes) else right
    dot = sum(a * b for a, b in zip(left_values, right_values, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _parent_path(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else path
