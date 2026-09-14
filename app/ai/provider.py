"""LLM provider abstraction.

Fallback chain:
  1. Groq models (fast LPU inference) — tries all models in order
  2. OpenRouter free models (fallback when Groq is exhausted)

Both use OpenAI-compatible APIs so we reuse the same SDK.
"""
from __future__ import annotations

import abc
import re
from typing import Any

from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError

from app.ai.schemas import LLMRequest, LLMResponse
from app.config.settings import get_settings
from app.utils.logging import get_logger

log = get_logger(__name__)


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResponse:
        ...


class ProviderError(Exception):
    """Raised when all providers fail."""


# ── Provider configs ──────────────────────────────────────────
GROQ_MODELS = [
    "openai/gpt-oss-20b",
    "gemma2-9b-it",
    "llama-3.1-8b-instant",
    "qwen/qwen3.6-27b",   # last — outputs <think> blocks
]

OPENROUTER_MODELS = [
    "meta-llama/llama-3.1-8b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "google/gemma-2-9b-it:free",
    "microsoft/phi-3-mini-128k-instruct:free",
]


def _strip_think(content: str) -> str:
    """Remove <think>...</think> blocks that some models output."""
    if "<think>" in content:
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return content


async def _call_model(
    client: AsyncOpenAI,
    model: str,
    kwargs: dict,
) -> LLMResponse:
    """Single model call. Raises RateLimitError or ProviderError."""
    try:
        completion = await client.chat.completions.create(**kwargs, model=model)
        choice = completion.choices[0]
        msg = choice.message

        # Strip <think> blocks (qwen and some other models output these)
        content = _strip_think(msg.content or "")

        tool_calls: list[dict[str, Any]] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })

        usage = completion.usage
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage_prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            usage_completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )

    except RateLimitError:
        raise  # Let caller handle fallback

    except APITimeoutError:
        raise ProviderError("LLM request timed out") from None

    except APIError as e:
        if "429" in str(e) or "rate_limit" in str(e).lower() or "rate limit" in str(e).lower():
            raise RateLimitError(response=None, body=None, message=str(e))  # type: ignore
        raise ProviderError(f"LLM API error: {e}") from e


class MultiProviderClient(LLMProvider):
    """Tries Groq models first, falls back to OpenRouter when all Groq models are rate limited."""

    def __init__(self) -> None:
        settings = get_settings()

        self._groq = AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            timeout=60.0,
            max_retries=1,
        )

        # OpenRouter — only init if key is set
        self._openrouter = None
        if settings.openrouter_api_key:
            self._openrouter = AsyncOpenAI(
                api_key=settings.openrouter_api_key,
                base_url="https://openrouter.ai/api/v1",
                timeout=60.0,
                max_retries=1,
            )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        kwargs = request.to_openai_kwargs()

        # ── Try Groq models first ──────────────────────────────
        for i, model in enumerate(GROQ_MODELS):
            try:
                response = await _call_model(self._groq, model, dict(kwargs))
                if i > 0:
                    log.info("groq_fallback_model_used", model=model, attempt=i + 1)
                else:
                    log.debug("groq_primary_model_used", model=model)
                return response
            except RateLimitError:
                log.warning("groq_model_rate_limited", model=model)
                continue
            except ProviderError:
                raise

        # ── All Groq models exhausted — try OpenRouter ─────────
        if self._openrouter is None:
            raise ProviderError("All Groq models rate limited and no OpenRouter key configured")

        log.warning("groq_exhausted_switching_to_openrouter")

        for i, model in enumerate(OPENROUTER_MODELS):
            try:
                response = await _call_model(self._openrouter, model, dict(kwargs))
                log.info("openrouter_model_used", model=model)
                return response
            except RateLimitError:
                log.warning("openrouter_model_rate_limited", model=model)
                continue
            except ProviderError:
                raise

        raise ProviderError("All Groq and OpenRouter models exhausted — try again later")


# ── Factory ───────────────────────────────────────────────────
_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        _provider = MultiProviderClient()
        settings = get_settings()
        has_openrouter = bool(settings.openrouter_api_key)
        log.info(
            "llm_provider_initialized",
            provider="groq+openrouter" if has_openrouter else "groq_only",
            groq_models=len(GROQ_MODELS),
            openrouter_models=len(OPENROUTER_MODELS) if has_openrouter else 0,
        )
    return _provider


async def dispose_provider() -> None:
    global _provider
    if _provider is not None:
        client = getattr(_provider, "_groq", None)
        if client:
            await client.close()
        or_client = getattr(_provider, "_openrouter", None)
        if or_client:
            await or_client.close()
        log.info("llm_provider_disposed")
    _provider = None