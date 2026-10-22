from __future__ import annotations

import pytest
from pydantic import ValidationError

from alfresco_curator.config import (
    BrokerSettings,
    LLMSettings,
    Settings,
    get_settings,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A developer .env on disk must not decide what these assertions see."""
    monkeypatch.chdir(tmp_path)
    for name in list(dict(**{k: v for k, v in __import__("os").environ.items()})):
        if name.startswith(
            (
                "ALFRESCO_",
                "LLM_",
                "OPENAI_",
                "GEMINI_",
                "ANTHROPIC_",
                "OLLAMA_",
                "ELASTICSEARCH_",
                "INDEXER_",
                "ACTIVEMQ_",
                "CURATOR_",
            )
        ):
            monkeypatch.delenv(name, raising=False)


def test_defaults_are_usable_without_any_environment():
    settings = Settings()
    assert settings.alfresco.base_url == "http://localhost:8080/alfresco"
    assert settings.elasticsearch.index == "alfresco"
    assert settings.indexer.poll_seconds == 15.0
    assert settings.llm.provider == "openai"


def test_environment_overrides_are_applied(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALFRESCO_BASE_URL", "https://acs.example.com/alfresco")
    monkeypatch.setenv("INDEXER_POLL_SECONDS", "30")
    settings = Settings()

    assert settings.alfresco.base_url == "https://acs.example.com/alfresco"
    assert settings.indexer.poll_seconds == 30.0


def test_secrets_are_not_exposed_by_repr(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALFRESCO_ADMIN_PASSWORD", "super-secret")
    settings = Settings()

    assert "super-secret" not in repr(settings)
    assert settings.alfresco.admin_password.get_secret_value() == "super-secret"


def test_fallback_models_parse_from_a_comma_separated_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_FALLBACK_MODELS", " a/one , b/two ,, ")
    assert LLMSettings().openai_fallback_models == ["a/one", "b/two"]


def test_unset_fallback_models_are_empty():
    assert LLMSettings().openai_fallback_models == []


def test_transform_queues_parse_from_a_comma_separated_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ACTIVEMQ_TRANSFORM_QUEUES", "q1,q2")
    assert BrokerSettings().transform_queues == ["q1", "q2"]


def test_unknown_provider_is_rejected_at_startup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_PROVIDER", "not-a-provider")
    with pytest.raises(ValidationError, match="LLM_PROVIDER"):
        LLMSettings()


def test_provider_is_normalised(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_PROVIDER", "  OpenAI  ")
    assert LLMSettings().provider == "openai"


def test_get_settings_is_cached():
    assert get_settings() is get_settings()
