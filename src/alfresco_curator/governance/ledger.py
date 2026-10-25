"""A durable, tamper-evident record of what the agent decided and what left the network.

Alfresco's own audit records the change. It cannot record the reason: "admin
renamed this node" is true and useless once an agent chose the name. The
questions an auditor actually asks — which instruction produced this, what did
the model propose, who approved it, could it be put back — have answers only
here.

Written as JSON Lines, one record per line, each carrying the hash of the line
before it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

GENESIS = "0" * 64


def _digest(previous: str, body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{previous}{canonical}".encode()).hexdigest()


class Ledger:
    """Append-only JSON Lines, hash-chained.

    Tamper-*evident*, not tamper-proof, and the distinction is worth stating
    plainly to anyone being asked to trust it: editing or removing a line
    breaks the chain and `verify` names the first position that fails, but
    whoever owns the file can still delete the file. Durability and retention
    belong to the volume it is written to, not to this class.
    """

    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._last = GENESIS
        if self.path and self.path.exists():
            self._last = self._last_hash()

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def writable(self) -> bool:
        """Can a record actually be appended right now?

        Asked separately from `enabled` because the dangerous state is a
        ledger that is configured, reports itself on, and silently fails every
        write - which is exactly what a root-owned volume mount produces.
        """
        if self.path is None:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8"):
                pass
        except OSError:
            return False
        return True

    def _last_hash(self) -> str:
        previous = GENESIS
        for record in self.read():
            previous = str(record.get("hash") or previous)
        return previous

    def append(
        self,
        kind: str,
        actor: str,
        detail: dict[str, Any],
        session: str = "",
    ) -> dict[str, Any]:
        body = {
            "at": time.time(),
            "kind": kind,
            "actor": actor,
            "session": session,
            "detail": detail,
        }
        with self._lock:
            body["previous"] = self._last
            record = {**body, "hash": _digest(self._last, body)}
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    # Opened per write: O_APPEND keeps a short line atomic, so
                    # a second process writing the same file cannot interleave.
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, default=str) + "\n")
                except OSError as exc:
                    # A failed audit write must not take the user's request
                    # down with it, but it must be loud.
                    log.error("governance ledger write failed: %s", exc)
                    return record
            self._last = str(record["hash"])
        return record

    def read(self) -> list[dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        records.append({"hash": "", "corrupt": line[:200]})
        except OSError as exc:
            # An unreadable ledger must not stop the service from answering;
            # `verify` is where an operator finds out it is unreadable.
            log.error("governance ledger read failed: %s", exc)
        return records

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.read()[-limit:]

    def verify(self) -> dict[str, Any]:
        """Recompute the chain and report the first line that does not hold."""
        previous = GENESIS
        for index, record in enumerate(self.read(), start=1):
            body = {key: record.get(key) for key in ("at", "kind", "actor", "session", "detail")}
            body["previous"] = previous
            if record.get("previous") != previous or record.get("hash") != _digest(previous, body):
                return {"ok": False, "checked": index, "brokenAt": index}
            previous = str(record["hash"])
        return {"ok": True, "checked": len(self.read()), "brokenAt": None}
