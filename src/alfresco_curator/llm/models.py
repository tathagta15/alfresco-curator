"""Which model to ask, and what to fall back to.

The HTTP calling lives in LangChain now; this is only the catalogue, kept
separate so the model chain can be reasoned about without touching transport.
"""

from __future__ import annotations

from alfresco_curator.config import LLMSettings

DEFAULT_MODELS = {
    "anthropic": "claude-3-5-sonnet-latest",
    "gemini": "gemini-3.8-flash",
    "ollama": "llama3.1",
}

# Free tiers rate limit per model, and individual models go 503 under load or
# turn out to be paid-only, so fall back across distinct families. Aliases such
# as gemini-flash-latest resolve to the same quota bucket and are useless here.
BUILTIN_FALLBACKS = {
    "openai": ["qwen/qwen3.5-flash:free", "qwen/qwen3.6-27b:free"],
    "gemini": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"],
}


def default_model(settings: LLMSettings) -> str:
    if settings.provider == "openai":
        # A custom gateway implies a gateway catalogue, not the OpenAI one.
        return "qwen/qwen3.7-flash:free" if settings.openai_base_url else "gpt-4o"
    return DEFAULT_MODELS.get(settings.provider, "gpt-4o")


def fallback_models(settings: LLMSettings) -> list[str]:
    configured = {
        "openai": settings.openai_fallback_models,
        "gemini": settings.gemini_fallback_models,
    }.get(settings.provider, [])
    return configured or BUILTIN_FALLBACKS.get(settings.provider, [])
