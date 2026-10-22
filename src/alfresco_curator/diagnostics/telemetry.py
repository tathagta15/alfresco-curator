"""Read-only telemetry sources behind the search pipeline.

Three independent systems have to be interrogated to explain why a document
is or is not searchable: the Elasticsearch index, the batch indexer's
actuator, and the ActiveMQ broker. Each probe returns observed values or an
error string; nothing here infers or guesses.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from alfresco_curator.config import Settings, get_settings

log = logging.getLogger(__name__)

# Per-module watermarks the indexer writes into each Elasticsearch document.
MODULE_WATERMARKS = {
    "metadata": "METADATA_INDEXING_LAST_UPDATE",
    "path": "PATH_INDEXING_LAST_UPDATE",
    "content": "CONTENT_INDEXING_LAST_UPDATE",
}

_JOLOKIA_MBEAN = (
    "org.apache.activemq:type=Broker,brokerName=localhost,"
    "destinationType=Queue,destinationName={queue}"
)


class Telemetry:
    """Probes Elasticsearch, the indexer actuator and the broker."""

    def __init__(self, settings: Settings | None = None, timeout: float = 15.0) -> None:
        config = settings or get_settings()
        self.es_url = config.elasticsearch.url.rstrip("/")
        self.es_index = config.elasticsearch.index
        self.indexer_url = config.indexer.url.rstrip("/")
        self.poll_seconds = config.indexer.poll_seconds
        self.stale_cycle_factor = config.indexer.stale_cycle_factor
        self.broker_url = config.broker.url.rstrip("/")
        self.transform_queues = tuple(config.broker.transform_queues)
        self._broker_auth = (
            config.broker.user,
            config.broker.password.get_secret_value(),
        )
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Telemetry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, url: str, **kwargs: Any) -> tuple[Any, str | None]:
        try:
            resp = self._http.get(url, **kwargs)
        except httpx.RequestError as exc:
            return None, f"unreachable: {exc}"
        if resp.status_code == 404:
            return None, None
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}"
        try:
            return resp.json(), None
        except ValueError:
            return None, "non-JSON response"

    # ----------------------------------------------------------- elasticsearch
    def cluster_health(self) -> dict[str, Any]:
        data, error = self._get(f"{self.es_url}/_cluster/health")
        return {"error": error} if error else data or {}

    def index_stats(self) -> dict[str, Any]:
        data, error = self._get(f"{self.es_url}/{self.es_index}/_count")
        if error:
            return {"error": error}
        return {"documents": (data or {}).get("count")}

    def document(self, node_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """The indexer stores each node under its UUID as the document _id."""
        data, error = self._get(f"{self.es_url}/{self.es_index}/_doc/{node_id}")
        if error:
            return None, error
        if not data or not data.get("found"):
            return None, None
        return data.get("_source", {}), None

    def analyze(self, text: str, field: str | None = None) -> dict[str, Any]:
        """Ask Elasticsearch how it actually splits a value into terms.

        A phrase query is matched against these tokens, not against the string
        the user typed, so a hyphenated or underscored name silently becomes
        several terms and an exact match stops working.
        """
        payload: dict[str, Any] = {"text": text}
        if field:
            payload["field"] = field
        else:
            payload["analyzer"] = "standard"
        try:
            resp = self._http.post(f"{self.es_url}/{self.es_index}/_analyze", json=payload)
        except httpx.RequestError as exc:
            return {"error": f"unreachable: {exc}"}
        if resp.status_code >= 400:
            # An unknown field is expected for properties this index never mapped.
            return {"error": f"HTTP {resp.status_code}"}
        try:
            tokens = [t["token"] for t in resp.json().get("tokens", [])]
        except ValueError:
            return {"error": "non-JSON response"}
        return {"text": text, "field": field, "tokens": tokens, "tokenCount": len(tokens)}

    # ----------------------------------------------------------------- indexer
    def indexer_health(self) -> dict[str, Any]:
        data, error = self._get(f"{self.indexer_url}/actuator/health")
        return {"error": error} if error else {"status": (data or {}).get("status")}

    def metric(self, name: str) -> dict[str, Any] | None:
        data, error = self._get(f"{self.indexer_url}/actuator/metrics/{name}")
        if error or not data:
            return None
        return {
            "measurements": {m["statistic"]: m["value"] for m in data.get("measurements", [])},
            "tags": {t["tag"]: t.get("values", []) for t in data.get("availableTags", [])},
            "description": data.get("description"),
        }

    def metric_value(self, name: str, statistic: str = "VALUE") -> float | None:
        metric = self.metric(name)
        if not metric:
            return None
        return metric["measurements"].get(statistic)

    def module_write_outcomes(self) -> dict[str, dict[str, float | None]]:
        """alfresco.reindex.write is tagged by module and by outcome."""
        outcomes: dict[str, dict[str, float | None]] = {}
        for module in MODULE_WATERMARKS:
            for outcome in ("success", "failure"):
                data, error = self._get(
                    f"{self.indexer_url}/actuator/metrics/alfresco.reindex.write",
                    params={"tag": [f"module:{module}", f"outcome:{outcome}"]},
                )
                if error or not data:
                    continue
                count = next(
                    (m["value"] for m in data.get("measurements", []) if m["statistic"] == "COUNT"),
                    None,
                )
                outcomes.setdefault(module, {})[outcome] = count
        return outcomes

    # ------------------------------------------------------------------ broker
    def queue_depth(self, queue: str) -> int | None:
        """Read one queue depth over Jolokia.

        Jolokia's default security policy blocks GET, and it answers 200 with
        an `"error"` body rather than an HTTP error code, so the payload has to
        be inspected instead of just the status.
        """
        payload = {
            "type": "read",
            "mbean": _JOLOKIA_MBEAN.format(queue=queue),
            "attribute": "QueueSize",
        }
        try:
            resp = self._http.post(
                f"{self.broker_url}/api/jolokia/",
                json=payload,
                auth=self._broker_auth,
                headers={"Origin": self.broker_url, "Content-Type": "application/json"},
            )
        except httpx.RequestError as exc:
            log.debug("broker unreachable: %s", exc)
            return None
        if resp.status_code >= 400:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        if body.get("error") or body.get("status") not in (200, None):
            log.debug("jolokia error for %s: %s", queue, body.get("error"))
            return None
        value = body.get("value")
        return int(value) if isinstance(value, (int, float)) else None

    def transform_queue_depths(self) -> dict[str, int | None]:
        return {queue: self.queue_depth(queue) for queue in self.transform_queues}
