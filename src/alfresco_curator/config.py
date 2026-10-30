"""Typed application settings.

Every environment variable the application reads is declared here, so
configuration is discoverable in one place and validated at startup rather
than failing deep inside a request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_BASE = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    frozen=True,
)


def _csv(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [item.strip() for item in value if str(item).strip()]
    return [item.strip() for item in value.split(",") if item.strip()]


class AlfrescoSettings(BaseSettings):
    model_config = _BASE | SettingsConfigDict(env_prefix="ALFRESCO_")

    base_url: str = "http://localhost:8080/alfresco"
    admin_user: str = "admin"
    admin_password: SecretStr = SecretStr("admin")
    timeout_seconds: float = 30.0
    verify_tls: bool = True
    # Which query language the repository speaks is a property of the
    # deployment, not a choice for whoever is searching. "auto" asks the
    # repository once; set it explicitly to pin it.
    query_language: Literal["auto", "afts", "lucene"] = "auto"


class ElasticsearchSettings(BaseSettings):
    model_config = _BASE | SettingsConfigDict(env_prefix="ELASTICSEARCH_")

    url: str = "http://localhost:9200"
    index: str = "alfresco"


class IndexerSettings(BaseSettings):
    model_config = _BASE | SettingsConfigDict(env_prefix="INDEXER_")

    url: str = "http://localhost:8081"
    poll_seconds: float = 15.0
    # A cycle older than poll_seconds * this factor is reported as lagging.
    stale_cycle_factor: float = 4.0


class BrokerSettings(BaseSettings):
    """ActiveMQ. Carries transform traffic only; the index is fed by DB polling."""

    model_config = _BASE | SettingsConfigDict(env_prefix="ACTIVEMQ_")

    url: str = "http://localhost:8161"
    user: str = "admin"
    password: SecretStr = SecretStr("admin")
    # NoDecode: these arrive as comma-separated strings, not JSON arrays, so
    # decoding must be left to the validator below.
    transform_queues: Annotated[list[str], NoDecode] = Field(
        default=["acs-repo-transform-request", "acs-repo-rendition-events"]
    )

    @field_validator("transform_queues", mode="before")
    @classmethod
    def _split(cls, value: str | list[str]) -> list[str]:
        return _csv(value)


class LLMSettings(BaseSettings):
    model_config = _BASE

    provider: str = Field(default="openai", alias="LLM_PROVIDER")
    model: str = Field(default="", alias="LLM_MODEL")

    openai_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="", alias="OPENAI_BASE_URL")
    openai_fallback_models: Annotated[list[str], NoDecode] = Field(
        default=[], alias="OPENAI_FALLBACK_MODELS"
    )

    gemini_api_key: SecretStr = Field(default=SecretStr(""), alias="GEMINI_API_KEY")
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta", alias="GEMINI_BASE_URL"
    )
    gemini_fallback_models: Annotated[list[str], NoDecode] = Field(
        default=[], alias="GEMINI_FALLBACK_MODELS"
    )

    anthropic_api_key: SecretStr = Field(default=SecretStr(""), alias="ANTHROPIC_API_KEY")
    ollama_base_url: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")

    request_timeout_seconds: float = Field(default=180.0, alias="LLM_TIMEOUT_SECONDS")
    max_attempts_per_model: int = Field(default=3, alias="LLM_MAX_ATTEMPTS")

    # A daily quota is charged per account, not per model, so every model on
    # one gateway fails together. A backup on a different provider is the only
    # fallback that survives that.
    backup_provider: str = Field(default="", alias="LLM_BACKUP_PROVIDER")
    backup_model: str = Field(default="", alias="LLM_BACKUP_MODEL")

    # LangSmith reads LANGSMITH_* from the environment itself; these exist so
    # the setting is declared in one place and can be echoed on /status.
    tracing_enabled: bool = Field(default=False, alias="LANGSMITH_TRACING")
    tracing_project: str = Field(default="alfresco-curator", alias="LANGSMITH_PROJECT")

    @field_validator("openai_fallback_models", "gemini_fallback_models", mode="before")
    @classmethod
    def _split(cls, value: str | list[str]) -> list[str]:
        return _csv(value)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        provider = value.strip().lower()
        allowed = {"openai", "anthropic", "gemini", "ollama"}
        if provider not in allowed:
            raise ValueError(f"LLM_PROVIDER must be one of {sorted(allowed)}, got {value!r}")
        return provider


class WebSettings(BaseSettings):
    model_config = _BASE | SettingsConfigDict(env_prefix="CURATOR_")

    session_ttl_seconds: int = 8 * 3600
    max_sessions: int = 200
    log_level: str = "INFO"

    # Where Share is reachable from the user's browser, used to build
    # "open the document" links out of search results.
    share_base_url: str = "http://localhost:8180/share"

    # Origins permitted to embed the UI in a frame. Share is listed by default
    # so the in-Share page works out of the box.
    frame_ancestors: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["'self'", "http://localhost:8180"]
    )

    @field_validator("frame_ancestors", mode="before")
    @classmethod
    def _split_ancestors(cls, value: str | list[str]) -> list[str]:
        return _csv(value)

    # SameSite compares registrable domain, not origin, so "strict" still
    # reaches a Share-hosted frame on a different port of the same host.
    # Genuinely cross-domain deployments need "none" plus Secure (HTTPS), or
    # better, the curator reverse-proxied under Share's own origin.
    cookie_samesite: Literal["strict", "lax", "none"] = "strict"
    cookie_secure: bool = False

    @model_validator(mode="after")
    def _samesite_none_needs_secure(self) -> WebSettings:
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError(
                "CURATOR_COOKIE_SAMESITE=none requires CURATOR_COOKIE_SECURE=true; "
                "browsers reject SameSite=None cookies sent without Secure."
            )
        return self


class SemanticSettings(BaseSettings):
    model_config = _BASE | SettingsConfigDict(env_prefix="SEMANTIC_")

    enabled: bool = True
    model_name: str = "BAAI/bge-small-en-v1.5"
    model_cache_dir: str = "data/embedding-models"
    index_path: str = "data/semantic-suggestions.sqlite3"
    max_documents: int = Field(default=100, ge=1, le=500)


class GovernanceSettings(BaseSettings):
    model_config = _BASE | SettingsConfigDict(env_prefix="GOVERNANCE_")

    # Durable by default: an audit trail that dies with the container is not
    # an audit trail. Mount this path to keep it across a restart.
    ledger_path: str = "data/governance-ledger.jsonl"
    policy_path: str = "data/governance-policy.json"

    # Alfresco group authorities allowed to change governance policy. A
    # dedicated group can replace the built-in administrators group.
    admin_groups: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["GROUP_ALFRESCO_ADMINISTRATORS"]
    )

    # Empty means unrestricted, which is reported on /api/health rather than
    # left to be discovered. When set, a model host outside it refuses to
    # build, so a non-compliant deployment never serves a first request.
    allowed_model_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Drop free-text metadata (title, description) before it reaches a model.
    minimise_metadata: bool = False
    mutation_budget: int = Field(default=25, ge=1, le=1000)
    allow_repository_changes: bool = True
    allow_access_changes: bool = True
    allow_repository_actions: bool = True

    @field_validator("allowed_model_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: str | list[str]) -> list[str]:
        return _csv(value)

    @field_validator("admin_groups", mode="before")
    @classmethod
    def _split_admin_groups(cls, value: str | list[str]) -> list[str]:
        return _csv(value)


class Settings(BaseSettings):
    model_config = _BASE

    alfresco: AlfrescoSettings = Field(default_factory=AlfrescoSettings)
    elasticsearch: ElasticsearchSettings = Field(default_factory=ElasticsearchSettings)
    indexer: IndexerSettings = Field(default_factory=IndexerSettings)
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    semantic: SemanticSettings = Field(default_factory=SemanticSettings)
    governance: GovernanceSettings = Field(default_factory=GovernanceSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
