"""Tool registry — central catalog of all tools available to the LLM.

Adding a new tool = create a class that subclasses BaseTool, register it in
`get_default_registry()`. Zero changes to orchestrator, prompts, or
conversation system.
"""
from __future__ import annotations

import abc
from typing import Any, Optional, Type

from pydantic import BaseModel, ValidationError

from app.tools.schemas import ToolContext, ToolResult, make_tool_schema
from app.utils.logging import get_logger

log = get_logger(__name__)


class BaseTool(abc.ABC):
    """Abstract base class for all tools.

    Subclasses must define:
      - name: short identifier used by the LLM (e.g. "create_reminder")
      - description: human-readable description shown to the LLM
      - args_model: Pydantic model class used for arg validation
      - parameters_schema: JSON schema dict for the LLM

    And implement:
      - check_permissions(ctx, args) -> Optional[str]
          Return None if allowed, else an error message string.
      - execute(args, ctx) -> ToolResult
          Perform the action. Return structured result.

    The framework calls validate_args() and check_permissions() before execute().
    """

    name: str = ""
    description: str = ""
    args_model: Type[BaseModel]
    parameters_schema: dict[str, Any]

    def validate_args(self, raw_args: dict[str, Any] | str) -> tuple[Optional[BaseModel], Optional[str]]:
        """Validate raw args from the LLM against our Pydantic model.

        Returns (parsed_model, None) on success, (None, error_message) on failure.
        """
        if isinstance(raw_args, str):
            # LLMs sometimes send args as a JSON string instead of a dict
            import json
            try:
                raw_args = json.loads(raw_args)
            except json.JSONDecodeError as e:
                return None, f"args were a string but not valid JSON: {e}"

        if not isinstance(raw_args, dict):
            return None, f"args must be a JSON object, got {type(raw_args).__name__}"

        try:
            parsed = self.args_model(**raw_args)
            return parsed, None
        except ValidationError as e:
            # Pydantic emits a structured error; we extract a readable summary
            errors = []
            for err in e.errors():
                loc = ".".join(str(x) for x in err.get("loc", []))
                msg = err.get("msg", "invalid")
                errors.append(f"{loc}: {msg}")
            return None, "; ".join(errors)

    def check_permissions(self, ctx: ToolContext, args: BaseModel) -> Optional[str]:
        """Override in subclasses. Return None if allowed, else error message."""
        return None

    @abc.abstractmethod
    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Perform the tool's action. Must return a ToolResult."""
        ...

    def to_openai_tool(self) -> dict[str, Any]:
        """Return the OpenAI-shape tool definition for the LLM."""
        return make_tool_schema(
            name=self.name,
            description=self.description,
            parameters=self.parameters_schema,
        )


# ── Registry ──────────────────────────────────────────────────
class ToolRegistry:
    """Holds all registered tools. Singleton via get_default_registry()."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if not tool.name:
            raise ValueError(f"Tool {tool.__class__.__name__} has no name")
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name} already registered")
        self._tools[tool.name] = tool
        log.info("tool_registered", name=tool.name)

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def all_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def all_schemas(self) -> list[dict[str, Any]]:
        """Return OpenAI-shape schemas for ALL registered tools — for the LLM."""
        return [t.to_openai_tool() for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# ── Singleton ─────────────────────────────────────────────────
_registry: ToolRegistry | None = None


def get_default_registry() -> ToolRegistry:
    """Get the singleton registry, initializing it on first call.

    Tools are conditionally registered based on feature flags in settings.
    """
    global _registry
    if _registry is not None:
        return _registry

    from app.config.settings import get_settings
    settings = get_settings()

    _registry = ToolRegistry()

    if settings.enable_tools:
        if settings.enable_reminders:
            from app.tools.reminders.service import (
                CreateReminderTool,
                ListRemindersTool,
                CancelReminderTool,
            )
            _registry.register(CreateReminderTool())
            _registry.register(ListRemindersTool())
            _registry.register(CancelReminderTool())

        if settings.enable_timers:
            from app.tools.timers.service import (
                StartTimerTool,
                ListTimersTool,
                CancelTimerTool,
            )
            _registry.register(StartTimerTool())
            _registry.register(ListTimersTool())
            _registry.register(CancelTimerTool())



    log.info("registry_initialized", tool_count=len(_registry))
    return _registry


def reset_registry() -> None:
    """Reset the singleton — used by tests."""
    global _registry
    _registry = None
