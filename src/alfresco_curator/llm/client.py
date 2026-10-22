"""Model access, built on LangChain chat models.

Retry and fallback are LangChain runnables rather than a hand-written loop:
`.with_retry()` covers transient faults and `.with_fallbacks()` walks the
model chain, so the policy is declared once and is visible to LangSmith
tracing. The two entry points keep their original signatures, because callers
care about "give me text" or "give me JSON", not how the model is reached.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import JsonOutputParser, StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from alfresco_curator.config import LLMSettings, get_settings
from alfresco_curator.errors import LLMError
from alfresco_curator.llm import models as catalogue

log = logging.getLogger(__name__)


def _settings() -> LLMSettings:
    return get_settings().llm


def provider() -> str:
    return _settings().provider


def model() -> str:
    settings = _settings()
    return settings.model.strip() or catalogue.default_model(settings)


def candidate_models() -> list[str]:
    settings = _settings()
    primary = model()
    return [primary] + [m for m in catalogue.fallback_models(settings) if m != primary]


def chat_model(
    name: str,
    *,
    temperature: float = 0.0,
    json_mode: bool = False,
    provider_name: str = "",
) -> BaseChatModel:
    """One configured LangChain chat model, without retry or fallback."""
    settings = _settings()
    which = provider_name or settings.provider
    kwargs: dict[str, Any] = {
        "model": name,
        "temperature": temperature,
        "timeout": settings.request_timeout_seconds,
    }

    if which == "openai":
        from langchain_openai import ChatOpenAI

        if json_mode:
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
        return ChatOpenAI(
            api_key=settings.openai_api_key.get_secret_value() or "not-set",
            base_url=settings.openai_base_url or None,
            max_retries=0,  # retries belong to the runnable, not the SDK
            **kwargs,
        )

    if which == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value() or "not-set",
            max_tokens=4096,
            **kwargs,
        )

    if which == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            google_api_key=settings.gemini_api_key.get_secret_value() or "not-set",
            # As with OpenAI above: the google-genai SDK otherwise runs its own
            # exponential backoff underneath the runnable, so a 429 that will
            # not clear for hours is retried for ~30s per call and the two
            # layers of retry multiply. Retries belong to the runnable, which
            # knows there is another provider to try.
            max_retries=0,
            **kwargs,
        )

    from langchain_ollama import ChatOllama

    return ChatOllama(
        base_url=settings.ollama_base_url, format="json" if json_mode else None, **kwargs
    )


def resilient_model(*, temperature: float = 0.0, json_mode: bool = False) -> Runnable:
    """The primary model with retries, falling back down the model chain.

    A free tier answers 429 for a daily cap, where waiting does not help, so
    exhausting the retries and moving to the next model is the useful shape.
    The cap is charged per account, so the chain ends on a different provider
    when one is configured - otherwise every fallback fails together.
    """
    settings = _settings()
    names = candidate_models()
    if not names or not names[0]:
        raise LLMError("no model configured - set LLM_MODEL")

    def build(name: str, provider_name: str = "") -> Runnable:
        return chat_model(
            name,
            temperature=temperature,
            json_mode=json_mode,
            provider_name=provider_name,
        ).with_retry(
            stop_after_attempt=max(1, settings.max_attempts_per_model),
            wait_exponential_jitter=True,
        )

    primary, rest = build(names[0]), [build(name) for name in names[1:]]
    if settings.backup_provider and settings.backup_model:
        rest.append(build(settings.backup_model, settings.backup_provider))
    return primary.with_fallbacks(rest) if rest else primary


# The system and user texts are passed as VALUES, never interpolated into the
# template. The prompts contain JSON worked examples, and their braces would
# otherwise be read as template variables.
_TEMPLATE = ChatPromptTemplate.from_messages([("system", "{system}"), ("human", "{user}")])


def text_chain(*, temperature: float = 0.0) -> Runnable:
    """prompt -> model -> text, as one traceable runnable."""
    return (_TEMPLATE | resilient_model(temperature=temperature) | StrOutputParser()).with_config(
        run_name="curator.text"
    )


def json_chain(*, temperature: float = 0.0) -> Runnable:
    """prompt -> model -> parsed JSON.

    JsonOutputParser already tolerates the ``` fences models add despite being
    told not to, so there is no hand-written stripping left on this path.
    """
    return (
        _TEMPLATE | resilient_model(temperature=temperature, json_mode=True) | JsonOutputParser()
    ).with_config(run_name="curator.json")


def _attempted() -> str:
    """Every model the chain would have tried, for an honest error message."""
    settings = _settings()
    names = list(candidate_models())
    if settings.backup_provider and settings.backup_model:
        names.append(f"{settings.backup_provider}:{settings.backup_model}")
    return ", ".join(names)


def _invoke(chain: Runnable, system: str, user: str) -> Any:
    try:
        return chain.invoke({"system": system, "user": user})
    except (LLMError, OutputParserException):
        raise
    except Exception as exc:
        raise LLMError(f"{provider()} call failed - {_brief(exc)} (tried: {_attempted()})") from exc


# Providers answer a quota failure with a page of JSON - the help links, the
# violation records, the retry delay in nanoseconds. None of it reaches a user
# who can act on it, and it ends up pasted into reports that are meant to be
# read, so the one fact worth keeping is lifted out.
_QUOTA = re.compile(r"429|quota|rate.?limit|RESOURCE_EXHAUSTED", re.IGNORECASE)
_RESET = re.compile(r"retry in (?:(\d+)h)?(?:(\d+)m)?", re.IGNORECASE)


def _brief(exc: Exception) -> str:
    text = str(exc)
    if not _QUOTA.search(text):
        return text.split("\n", 1)[0][:300]

    reset = _RESET.search(text)
    labelled = []
    if reset:
        if reset.group(1):
            labelled.append(f"{reset.group(1)}h")
        if reset.group(2):
            labelled.append(f"{reset.group(2)}m")
    window = f", resets in about {' '.join(labelled)}" if labelled else ""
    return f"the daily model quota is exhausted{window}"


def complete(
    system: str,
    user: str,
    *,
    json_mode: bool = False,
    temperature: float = 0.0,
) -> str:
    chain = (
        json_chain(temperature=temperature) if json_mode else text_chain(temperature=temperature)
    )
    result = _invoke(chain, system, user)
    text = result if isinstance(result, str) else json.dumps(result)
    if not text.strip():
        raise LLMError(f"{provider()} returned an empty response")
    return text


def stream_text(system: str, user: str, *, temperature: float = 0.0) -> Iterator[str]:
    """The same answer as `complete`, in the pieces it arrives in.

    The fallback chain streams from whichever model answers first, so a
    provider failing mid-stream is not recoverable here the way a failed
    invoke is - a caller that has already shown half an answer cannot take it
    back. Callers therefore keep `complete` for anything that must be atomic.
    """
    chain = text_chain(temperature=temperature)
    try:
        for chunk in chain.stream({"system": system, "user": user}):
            if chunk:
                yield chunk if isinstance(chunk, str) else str(chunk)
    except Exception as exc:
        raise LLMError(
            f"{provider()} stream failed - {_brief(exc)} (tried: {_attempted()})"
        ) from exc


def complete_json(system: str, user: str, *, temperature: float = 0.0) -> dict[str, Any]:
    try:
        parsed = _invoke(json_chain(temperature=temperature), system, user)
    except OutputParserException as exc:
        raise LLMError(f"Model did not return valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMError(f"Model returned {type(parsed).__name__}, expected a JSON object")
    return parsed


def strip_code_fences(text: str) -> str:
    """Kept for callers that hold raw model text; the JSON path no longer needs it."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()
