"""Where the data may go, and how little of it may go there.

Two controls a European deployment is asked for by name: the model endpoint is
restricted to hosts the operator has approved, and the metadata sent to it can
be reduced to what the answer actually needs.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from alfresco_curator.config import LLMSettings, Settings, get_settings
from alfresco_curator.errors import CuratorError

# Free-text metadata is where names, case numbers and client references end up.
# Dropped first when a deployment minimises; `name` and `path` are kept because
# an answer that cannot identify the document is not an answer.
SENSITIVE_FIELDS = ("title", "description")

# Where each provider sends a request when no base URL is configured.
DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}


class PolicyError(CuratorError):
    """Configuration that governance forbids. Raised at construction, not call time."""


def endpoint_for(provider: str, settings: LLMSettings | None = None) -> str:
    llm = settings or get_settings().llm
    if provider == "openai":
        return llm.openai_base_url or DEFAULT_ENDPOINTS["openai"]
    if provider == "gemini":
        return llm.gemini_base_url or DEFAULT_ENDPOINTS["gemini"]
    if provider == "ollama":
        return llm.ollama_base_url
    return DEFAULT_ENDPOINTS.get(provider, "")


def host_of(url: str) -> str:
    parsed = urlparse(url if "//" in url else f"//{url}")
    return (parsed.hostname or "").lower()


def assert_endpoint_allowed(provider: str, settings: Settings | None = None) -> None:
    """Fail closed when the model host is not on the operator's allowlist.

    Raised while the model is being built, so a non-compliant deployment never
    reaches a first request. An empty allowlist means unrestricted, which is
    reported on `/api/health` rather than left to be discovered.
    """
    config = settings or get_settings()
    allowed = config.governance.allowed_model_hosts
    if not allowed:
        return
    host = host_of(endpoint_for(provider, config.llm))
    if host not in {entry.lower() for entry in allowed}:
        raise PolicyError(
            f"model endpoint {host or provider!r} is not in GOVERNANCE_ALLOWED_MODEL_HOSTS "
            f"({', '.join(allowed)}); refusing to send repository metadata to it"
        )


def minimise(record: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """Strip the free-text fields when the deployment asks for minimisation."""
    config = settings or get_settings()
    if not config.governance.minimise_metadata:
        return record
    return {key: value for key, value in record.items() if key not in SENSITIVE_FIELDS}
