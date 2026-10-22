"""Alfresco Curator."""

from alfresco_curator.errors import (
    AlfrescoError,
    ConfigurationError,
    CuratorError,
    LLMError,
    QueryValidationError,
)

__version__ = "1.0.0"

__all__ = [
    "AlfrescoError",
    "ConfigurationError",
    "CuratorError",
    "LLMError",
    "QueryValidationError",
    "__version__",
]
