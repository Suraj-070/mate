"""Pydantic schemas shared across the AI layer.

These are wire-shape contracts between modules. They are NOT the database
models (those live in database/models.py) and they are NOT the LLM tool
schemas (those live in tools/schemas.py — Phase 3).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Response decision ─────────────────────────────────────────
class DecisionOutcome(str, Enum):
    """What the bot should do with this message."""

    IGNORE = "ignore"                  # do nothing — silent
    REACT = "react"                     # add an emoji reaction (Phase 2)
    RESPOND = "respond"                 # generate a reply
    EXECUTE_TOOL = "execute_tool"       # LLM likely wants to call a tool (Phase 3)
    ASK_CLARIFICATION = "ask_clarification"  # ambiguous request, ask user to clarify


class DecisionInput(BaseModel):
    """Inputs to the response decision layer."""

    is_direct_mention: bool = False
    is_reply_to_bot: bool = False
    is_reply_to_other: bool = False
    bot_spoke_recently: bool = False
    is_question: bool = False
    addressed_to_bot_by_name: bool = False
    channel_autonomy_enabled: bool = False
    cooldown_active: bool = False
    rate_limit_exceeded: bool = False
    author_opted_out: bool = False
    message_length: int = 0
    message_text: str = ""  # for REACT heuristic — the actual message content
    enable_react_outcome: bool = True


class DecisionResult(BaseModel):
    """The decision + reasoning trace (for debugging / observability)."""

    outcome: DecisionOutcome
    reason: str
    debug: dict[str, Any] = Field(default_factory=dict)


# ── LLM request / response ────────────────────────────────────
class LLMMessage(BaseModel):
    role: str  # 'system' | 'user' | 'assistant' | 'tool'
    content: Optional[str] = None
    name: Optional[str] = None  # for user attribution
    tool_calls: Optional[list[dict[str, Any]]] = None  # raw OpenAI-shape tool calls
    tool_call_id: Optional[str] = None

    def to_openai_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            d["content"] = self.content
        if self.name is not None:
            d["name"] = self.name
        if self.tool_calls is not None:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        return d


class LLMRequest(BaseModel):
    """Assembled request ready to send to provider."""

    system_prompt: str
    messages: list[LLMMessage] = Field(default_factory=list)
    max_tokens: int = 400
    temperature: float = 0.7
    tools: list[dict[str, Any]] = Field(default_factory=list)  # OpenAI tool schemas

    def to_openai_kwargs(self) -> dict[str, Any]:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        msgs.extend(m.to_openai_dict() for m in self.messages)
        kwargs: dict[str, Any] = {
            "messages": msgs,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if self.tools:
            kwargs["tools"] = self.tools
        return kwargs


class LLMResponse(BaseModel):
    """Parsed response from provider."""

    content: Optional[str] = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage_prompt_tokens: int = 0
    usage_completion_tokens: int = 0

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)
