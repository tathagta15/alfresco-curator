"""Where the data may go, and how little of it may go there.

Two controls a European deployment is asked for by name: the model endpoint is
restricted to hosts the operator has approved, and the metadata sent to it can
be reduced to what the answer actually needs.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import threading
from typing import Any
from urllib.parse import urlparse

from alfresco_curator.config import LLMSettings, Settings, get_settings
from alfresco_curator.errors import CuratorError

# Free-text metadata is where names, case numbers and client references end up.
# Dropped first when a deployment minimises; `name` and `path` are kept because
# an answer that cannot identify the document is not an answer.
SENSITIVE_FIELDS = ("title", "description")
_POLICY_LOCK = threading.RLock()
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# Where each provider sends a request when no base URL is configured.
DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}
ACCESS_CHANGE_ACTIONS = frozenset(
    {
        "grant_permission",
        "create_shared_link",
        "create_group",
        "add_group_member",
        "create_site",
        "add_site_member",
    }
)


class PolicyError(CuratorError):
    """Configuration that governance forbids. Raised at construction, not call time."""


def normalise_hosts(hosts: list[str]) -> list[str]:
    """Accept exact hostnames and IP addresses only, never URL fragments."""
    if len(hosts) > 50:
        raise ValueError("At most 50 model hosts may be allowed")
    result: list[str] = []
    for raw in hosts:
        host = raw.strip().lower().rstrip(".")
        if not host or any(char in host for char in "/@?#"):
            raise ValueError(f"Expected a hostname or IP address, got {raw!r}")
        try:
            host = ipaddress.ip_address(host.strip("[]")).compressed
        except ValueError:
            if len(host) > 253 or not all(
                _HOST_LABEL.fullmatch(label) for label in host.split(".")
            ):
                raise ValueError(f"Invalid model host: {raw!r}") from None
        if host not in result:
            result.append(host)
    return result


def _policy_path(settings: Settings | None = None):
    config = settings or get_settings()
    return config.governance.policy_path


def _read_override(settings: Settings | None = None) -> dict[str, Any]:
    path = _policy_path(settings)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"governance policy file is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("governance policy file must contain a JSON object")
    override: dict[str, Any] = {}
    if "allowed_model_hosts" in data:
        hosts = data["allowed_model_hosts"]
        if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
            raise PolicyError("governance policy allowed_model_hosts must be a string list")
        try:
            override["allowed_model_hosts"] = normalise_hosts(hosts)
        except ValueError as exc:
            raise PolicyError(f"governance policy has invalid model hosts: {exc}") from exc
    if "minimise_metadata" in data:
        value = data["minimise_metadata"]
        if not isinstance(value, bool):
            raise PolicyError("governance policy minimise_metadata must be boolean")
        override["minimise_metadata"] = value
    if "trace_to_third_party" in data:
        value = data["trace_to_third_party"]
        if not isinstance(value, bool):
            raise PolicyError("governance policy trace_to_third_party must be boolean")
        override["trace_to_third_party"] = value
    if "mutation_budget" in data:
        value = data["mutation_budget"]
        if type(value) is not int or not 1 <= value <= 1000:
            raise PolicyError("governance policy mutation_budget must be an integer from 1 to 1000")
        override["mutation_budget"] = value
    for key in (
        "allow_repository_changes",
        "allow_access_changes",
        "allow_repository_actions",
    ):
        if key in data:
            value = data[key]
            if not isinstance(value, bool):
                raise PolicyError(f"governance policy {key} must be boolean")
            override[key] = value
    return override


def effective_policy(settings: Settings | None = None) -> dict[str, Any]:
    """Read the durable operator override over the deployment defaults."""
    config = settings or get_settings()
    override = _read_override(config)
    raw_hosts = override.get("allowed_model_hosts", config.governance.allowed_model_hosts)
    try:
        allowed_hosts = normalise_hosts(raw_hosts)
    except ValueError as exc:
        raise PolicyError(f"deployment model host allowlist is invalid: {exc}") from exc
    return {
        "allowed_model_hosts": allowed_hosts,
        "minimise_metadata": override.get("minimise_metadata", config.governance.minimise_metadata),
        "trace_to_third_party": override.get("trace_to_third_party", config.llm.tracing_enabled),
        "mutation_budget": override.get("mutation_budget", config.governance.mutation_budget),
        "allow_repository_changes": override.get(
            "allow_repository_changes", config.governance.allow_repository_changes
        ),
        "allow_access_changes": override.get(
            "allow_access_changes", config.governance.allow_access_changes
        ),
        "allow_repository_actions": override.get(
            "allow_repository_actions", config.governance.allow_repository_actions
        ),
        "overridden": bool(override),
    }


def change_denial(action: str) -> str:
    """Return the policy reason to deny a mutation, or an empty string."""
    policy = effective_policy()
    if not policy.get("allow_repository_changes", True):
        return "Repository changes are paused by governance policy."
    if action in ACCESS_CHANGE_ACTIONS and not policy.get("allow_access_changes", True):
        return "Sharing and access changes are disabled by governance policy."
    if action in {"run_action", "create_folder_rule"} and not policy.get(
        "allow_repository_actions", True
    ):
        return "Alfresco repository actions are disabled by governance policy."
    return ""


def write_policy_override(
    allowed_hosts: list[str],
    minimise_metadata: bool,
    settings: Settings | None = None,
    *,
    trace_to_third_party: bool | None = None,
    mutation_budget: int | None = None,
    allow_repository_changes: bool | None = None,
    allow_access_changes: bool | None = None,
    allow_repository_actions: bool | None = None,
) -> None:
    path = _policy_path(settings)
    if not path:
        raise PolicyError("governance policy persistence is not configured")
    budget = (
        effective_policy(settings)["mutation_budget"]
        if mutation_budget is None
        else mutation_budget
    )
    if type(budget) is not int or not 1 <= budget <= 1000:
        raise ValueError("mutation_budget must be an integer from 1 to 1000")
    payload = {
        "allowed_model_hosts": normalise_hosts(allowed_hosts),
        "minimise_metadata": bool(minimise_metadata),
        "trace_to_third_party": (
            effective_policy(settings)["trace_to_third_party"]
            if trace_to_third_party is None
            else bool(trace_to_third_party)
        ),
        "mutation_budget": budget,
        "allow_repository_changes": (
            effective_policy(settings)["allow_repository_changes"]
            if allow_repository_changes is None
            else bool(allow_repository_changes)
        ),
        "allow_access_changes": (
            effective_policy(settings)["allow_access_changes"]
            if allow_access_changes is None
            else bool(allow_access_changes)
        ),
        "allow_repository_actions": (
            effective_policy(settings)["allow_repository_actions"]
            if allow_repository_actions is None
            else bool(allow_repository_actions)
        ),
    }
    destination = os.path.abspath(path)
    directory = os.path.dirname(destination)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temp_path = ""
    with _POLICY_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, prefix=".governance-", delete=False
            ) as handle:
                temp_path = handle.name
                os.chmod(temp_path, 0o600)
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, destination)
        except OSError as exc:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
            raise PolicyError(f"could not persist governance policy: {exc}") from exc


def reset_policy_override(settings: Settings | None = None) -> None:
    path = _policy_path(settings)
    if not path:
        raise PolicyError("governance policy persistence is not configured")
    with _POLICY_LOCK:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PolicyError(f"could not reset governance policy: {exc}") from exc


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
    allowed = effective_policy(config)["allowed_model_hosts"]
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
    if not effective_policy(settings)["minimise_metadata"]:
        return record
    return {key: value for key, value in record.items() if key not in SENSITIVE_FIELDS}
