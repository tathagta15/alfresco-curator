"""Provider-agnostic access to a chat model, over LangChain."""

from alfresco_curator.errors import LLMError
from alfresco_curator.llm.client import (
    candidate_models,
    chat_model,
    complete,
    complete_json,
    model,
    provider,
    resilient_model,
    stream_text,
    strip_code_fences,
)

__all__ = [
    "LLMError",
    "candidate_models",
    "chat_model",
    "complete",
    "complete_json",
    "model",
    "provider",
    "resilient_model",
    "stream_text",
    "strip_code_fences",
]
