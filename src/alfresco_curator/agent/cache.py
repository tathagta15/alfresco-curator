"""Short-lived memo cache shared by the agent's probes.

A single agent run asks the same questions repeatedly - the same clause count
while narrowing, the same telemetry across several pipeline stages - and a
follow-up question in the same session usually re-treads most of them. Results
are cached per key for a few seconds so a run costs one call per distinct
question rather than one per use.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


class TTLCache:
    """Thread-safe cache with a per-entry expiry, keyed by an opaque string."""

    def __init__(self, ttl_seconds: float = 30.0, max_entries: int = 512) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get_or_call(self, key: str, produce: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            found = self._entries.get(key)
            if found is not None and now - found[0] < self.ttl:
                self.hits += 1
                return found[1]

        # Produced outside the lock: a probe is a network call, and holding the
        # lock across it would serialise every other lookup behind it.
        value = produce()

        with self._lock:
            self.misses += 1
            if len(self._entries) >= self.max_entries:
                self._evict_expired(now)
            self._entries[key] = (now, value)
        return value

    def _evict_expired(self, now: float) -> None:
        stale = [k for k, (at, _) in self._entries.items() if now - at >= self.ttl]
        for key in stale:
            del self._entries[key]
        if len(self._entries) >= self.max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest]

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses, "entries": len(self._entries)}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
