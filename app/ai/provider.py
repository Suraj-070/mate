"""LLM provider abstraction.

This is the ONLY file that imports the openai SDK. All other modules talk
to LLMs through the LLMProvider interface, so we can swap providers by
changing this one file.

Groq exposes an OpenAI-compatible API at https://api.groq.com/openai/v1
so we reuse the openai SDK pointed at that base URL.
"""
from __future__ import annotations

import abc
from typing import Any

from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError

from app.ai.schemas import LLMRequest, LLMResponse
from app.config.settings import get_settings
from app.utils.logging import get_logger

log = get_logger(__name__)


class LLMProvider(abc.ABC):
    """Provider interface. Implement this to add a new LLM backend."""

    @abc.abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResponse:
        ...


class GroqProvider(LLMProvider):
    """Groq via the OpenAI-compatible API.

    Reuses the openai SDK by pointing its base_url at Groq.
    Supports: tool calling, streaming (not used in v1).

    Recommended models:
        llama-3.3-70b-versatile  — best quality, handles EN/Nepali well
        llama3-8b-8192           — faster / lower cost
        mixtral-8x7b-32768       — long context (32k)
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            timeout=60.0,
            max_retries=2,
        )
        self._model = settings.groq_model

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Call the LLM. Returns parsed response. Raises on hard failures."""
        kwargs = request.to_openai_kwargs()
        kwargs["model"] = self._model

        try:
            completion = await self._client.chat.completions.create(**kwargs)
        except APITimeoutError:
            log.error("provider_timeout", model=self._model)
            raise ProviderError("LLM request timed out") from None
        except RateLimitError:
            log.warning("provider_rate_limited", model=self._model)
            raise ProviderError("LLM provider rate-limited us") from None
        except APIError as e:
            log.error("provider_api_error", model=self._model, message=str(e))
            raise ProviderError(f"LLM API error: {e}") from e

        choice = completion.choices[0]
        msg = choice.message

        tool_calls: list[dict[str, Any]] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )

        usage = completion.usage
        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage_prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            usage_completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )


class ProviderError(Exception):
    """Raised when the LLM provider fails irrecoverably."""


# ── Factory ──────────────────────────────────────────────────
_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """Singleton provider. Constructed on first use."""
    global _provider
    if _provider is None:
        _provider = GroqProvider()
        log.info("llm_provider_initialized", provider="groq", model=get_settings().groq_model)
    return _provider


async def dispose_provider() -> None:
    """Shutdown hook — close the underlying httpx client."""
    global _provider
    if _provider is not None and hasattr(_provider, "_client"):
        await _provider._client.close()  # type: ignore[attr-defined]
        log.info("llm_provider_disposed")
    _provider = None