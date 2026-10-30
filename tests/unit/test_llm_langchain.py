"""The LLM layer is LangChain, so retry and fallback are runnable policy."""

from __future__ import annotations

import re
from contextlib import nullcontext
from unittest.mock import Mock, patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from alfresco_curator import llm
from alfresco_curator.errors import LLMError


class TestChatModel:
    def test_an_openai_gateway_keeps_its_base_url(self):
        with patch("alfresco_curator.llm.client.get_settings") as settings:
            cfg = settings.return_value.llm
            cfg.provider = "openai"
            cfg.openai_base_url = "https://gateway.example/v1"
            cfg.openai_api_key.get_secret_value.return_value = "sk-test"
            cfg.request_timeout_seconds = 30.0
            built = llm.chat_model("some/model")
        assert str(built.openai_api_base).rstrip("/") == "https://gateway.example/v1"

    def test_sdk_retries_are_disabled_so_the_runnable_owns_them(self):
        with patch("alfresco_curator.llm.client.get_settings") as settings:
            cfg = settings.return_value.llm
            cfg.provider = "openai"
            cfg.openai_base_url = ""
            cfg.openai_api_key.get_secret_value.return_value = "sk-test"
            cfg.request_timeout_seconds = 30.0
            built = llm.chat_model("gpt-4o")
        assert built.max_retries == 0, "two retry layers would multiply the wait"

    def test_gemini_sdk_retries_are_disabled_too(self):
        """The google-genai SDK defaults to six retries with exponential backoff.

        Left on, a daily-quota 429 - which will not clear for hours - was
        retried for about thirty seconds per call underneath the runnable,
        so the whole chain took over a minute to report a failure that was
        knowable immediately.
        """
        with patch("alfresco_curator.llm.client.get_settings") as settings:
            cfg = settings.return_value.llm
            cfg.provider = "gemini"
            cfg.gemini_api_key.get_secret_value.return_value = "key-test"
            cfg.request_timeout_seconds = 30.0
            built = llm.chat_model("gemini-3.6-flash")
        assert built.max_retries == 0

    def test_a_model_that_cannot_be_wrapped_may_keep_sdk_retries(self):
        """`bind_tools` is not exposed by a retry or fallback runnable, so the
        deep agent's model carries its own retries or has none at all. A
        transient 503 from the backup ended the turn with a 500."""
        with patch("alfresco_curator.llm.client.get_settings") as settings:
            cfg = settings.return_value.llm
            cfg.provider = "openai"
            cfg.openai_api_key.get_secret_value.return_value = "key-test"
            cfg.openai_base_url = ""
            cfg.request_timeout_seconds = 30.0
            built = llm.chat_model("gpt-4o", max_retries=2)
        assert built.max_retries == 2


class TestComplete:
    """The chain is prompt -> model -> parser, so the model is stubbed as a runnable."""

    def _settings(self, settings: Mock, attempts: int = 2) -> None:
        cfg = settings.return_value.llm
        cfg.provider = "openai"
        cfg.model = "primary"
        cfg.max_attempts_per_model = attempts
        cfg.openai_fallback_models = ["secondary"]
        cfg.gemini_fallback_models = []

    def test_a_reply_is_returned_as_text(self):
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.resilient_model") as chain,
        ):
            self._settings(settings)
            chain.return_value = RunnableLambda(lambda _: AIMessage(content="hello"))
            assert llm.complete("system", "user") == "hello"

    def test_model_call_uses_the_current_tracing_policy(self):
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.resilient_model") as chain,
            patch(
                "alfresco_curator.llm.client.effective_policy",
                return_value={"trace_to_third_party": True},
            ),
            patch(
                "alfresco_curator.llm.client.tracing_context", return_value=nullcontext()
            ) as trace,
        ):
            self._settings(settings)
            chain.return_value = RunnableLambda(lambda _: AIMessage(content="hello"))
            assert llm.complete("system", "user") == "hello"
        trace.assert_called_once_with(enabled=True)

    def test_a_failing_model_is_reported_with_what_was_tried(self):
        def explode(_: object) -> AIMessage:
            raise RuntimeError("429 quota")

        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.resilient_model") as chain,
        ):
            self._settings(settings)
            chain.return_value = RunnableLambda(explode)
            with pytest.raises(LLMError, match="primary, secondary"):
                llm.complete("system", "user")

    def test_an_empty_reply_is_an_error_not_an_answer(self):
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.resilient_model") as chain,
        ):
            self._settings(settings)
            chain.return_value = RunnableLambda(lambda _: AIMessage(content="   "))
            with pytest.raises(LLMError, match="empty"):
                llm.complete("system", "user")

    def test_content_blocks_are_joined(self):
        # Anthropic and Gemini answer with blocks rather than a plain string;
        # StrOutputParser is what flattens them.
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.resilient_model") as chain,
        ):
            self._settings(settings)
            chain.return_value = RunnableLambda(
                lambda _: AIMessage(
                    content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
                )
            )
            assert llm.complete("system", "user") == "ab"

    def test_json_is_parsed_through_the_chain(self):
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.resilient_model") as chain,
        ):
            self._settings(settings)
            chain.return_value = RunnableLambda(lambda _: AIMessage(content='{"afts_query": "x"}'))
            assert llm.complete_json("system", "user") == {"afts_query": "x"}

    def test_fenced_json_is_tolerated_without_hand_stripping(self):
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.resilient_model") as chain,
        ):
            self._settings(settings)
            chain.return_value = RunnableLambda(
                lambda _: AIMessage(content='```json\n{"afts_query": "x"}\n```')
            )
            assert llm.complete_json("system", "user") == {"afts_query": "x"}

    def test_a_prompt_containing_braces_is_not_treated_as_a_template(self):
        # The system prompts carry JSON worked examples; those braces must
        # survive rather than be read as template variables.
        seen: dict[str, str] = {}

        def capture(value: object) -> AIMessage:
            seen["text"] = value.to_messages()[0].content
            return AIMessage(content="ok")

        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.resilient_model") as chain,
        ):
            self._settings(settings)
            chain.return_value = RunnableLambda(capture)
            llm.complete('respond with {"afts_query": "..."}', "user")

        assert seen["text"] == 'respond with {"afts_query": "..."}'


class TestResilientModel:
    def test_the_fallback_chain_follows_the_configured_models(self):
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.chat_model") as build,
        ):
            cfg = settings.return_value.llm
            cfg.provider = "openai"
            cfg.model = "primary"
            cfg.max_attempts_per_model = 3
            cfg.openai_fallback_models = ["second", "third"]
            cfg.gemini_fallback_models = []
            cfg.backup_provider = ""
            llm.resilient_model()

        assert [call.args[0] for call in build.call_args_list] == ["primary", "second", "third"]

    def test_a_single_model_needs_no_fallback_wrapper(self):
        # anthropic has no built-in fallback chain, so the list is genuinely one.
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.chat_model") as build,
        ):
            cfg = settings.return_value.llm
            cfg.provider = "anthropic"
            cfg.model = "only"
            cfg.max_attempts_per_model = 1
            cfg.backup_provider = ""
            chain = llm.resilient_model()

        retried = build.return_value.with_retry.return_value
        retried.with_fallbacks.assert_not_called()
        assert chain is retried

    def test_an_unset_model_still_resolves_a_default(self):
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.chat_model") as build,
        ):
            cfg = settings.return_value.llm
            cfg.provider = "openai"
            cfg.model = "   "
            cfg.openai_base_url = "https://gateway.example/v1"
            cfg.max_attempts_per_model = 1
            cfg.openai_fallback_models = []
            cfg.gemini_fallback_models = []
            cfg.backup_provider = ""
            llm.resilient_model()

        assert build.call_args_list[0].args[0] == "qwen/qwen3.7-flash:free"


class TestCrossProviderBackup:
    """A daily quota is charged per account, so same-gateway fallbacks all fail together."""

    def _settings(self, settings: Mock, backup: tuple[str, str] | None) -> None:
        cfg = settings.return_value.llm
        cfg.provider = "openai"
        cfg.model = "primary"
        cfg.max_attempts_per_model = 1
        cfg.openai_fallback_models = ["secondary"]
        cfg.gemini_fallback_models = []
        cfg.backup_provider, cfg.backup_model = backup or ("", "")

    def test_the_backup_is_built_on_the_other_provider(self):
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.chat_model") as build,
        ):
            self._settings(settings, ("gemini", "gemini-3.6-flash"))
            llm.resilient_model()

        last = build.call_args_list[-1]
        assert last.args[0] == "gemini-3.6-flash"
        assert last.kwargs["provider_name"] == "gemini"

    def test_the_backup_comes_last(self):
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.chat_model") as build,
        ):
            self._settings(settings, ("gemini", "gemini-3.6-flash"))
            llm.resilient_model()

        assert [c.args[0] for c in build.call_args_list] == [
            "primary",
            "secondary",
            "gemini-3.6-flash",
        ]

    def test_no_backup_configured_leaves_the_chain_alone(self):
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.chat_model") as build,
        ):
            self._settings(settings, None)
            llm.resilient_model()

        assert [c.args[0] for c in build.call_args_list] == ["primary", "secondary"]

    def test_the_error_names_the_backup_that_was_tried(self):
        # Without this the message blames the primary for a whole-chain failure.
        with (
            patch("alfresco_curator.llm.client.get_settings") as settings,
            patch("alfresco_curator.llm.client.chat_model"),
            patch("alfresco_curator.llm.client.resilient_model") as chain,
        ):
            self._settings(settings, ("gemini", "gemini-3.6-flash"))
            chain.return_value = RunnableLambda(
                lambda _: (_ for _ in ()).throw(RuntimeError("429 quota"))
            )
            with pytest.raises(LLMError, match=re.escape("gemini:gemini-3.6-flash")):
                llm.complete("system", "user")
