"""Weather tool — example custom HTTP integration (Phase 4).

Uses wttr.in — a free, no-API-key weather service. Returns plain-text weather
for a given location. The LLM never sees the raw HTTP response — only the
parsed + summarized ToolResult.

Adding a new HTTP tool = copy this file, change the host + URL builder +
response parser. The framework handles everything else.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.tools.custom.base_http import BaseHTTPTool
from app.tools.schemas import ToolContext, ToolResult


class WeatherArgs(BaseModel):
    """Args for the weather tool.

    `location` can be a city name, airport code, or "~lat,lon" for coordinates.
    Examples: "Sydney", "~Tokyo", "~33.86,151.21"
    """

    location: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="City name or coordinates. Examples: 'Sydney', '~Tokyo', '~33.86,151.21'",
    )


class WeatherTool(BaseHTTPTool):
    """Get the current weather for a location via wttr.in (no API key needed)."""

    name = "get_weather"
    description = (
        "Get the current weather for a given location. Returns temperature, "
        "conditions, and a brief forecast. Useful when someone asks 'what's "
        "the weather like' or 'is it going to rain today'."
    )
    args_model = WeatherArgs
    parameters_schema = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "City name or '~lat,lon' coordinates. e.g. 'Sydney', '~Tokyo', '~33.86,151.21'",
            },
        },
        "required": ["location"],
    }

    host = "wttr.in"
    allowed_methods = {"GET"}
    require_https = True

    def build_url(self, args: WeatherArgs) -> str:
        # wttr.in format=3 returns a compact one-line summary:
        # "Sydney: ⛅️ +25°C"
        # We fetch the compact format and let the LLM expand it for the user.
        # Sanitize location: strip any URL-special chars to prevent path injection
        import urllib.parse
        safe_location = urllib.parse.quote(args.location.strip(), safe="~,-")
        return f"/{safe_location}?format=3"

    def parse_response(self, body: str, args: WeatherArgs, ctx: ToolContext) -> ToolResult:
        """Parse wttr.in's compact response.

        The body looks like:
          "Sydney: ⛅️ +25°C\n"
          or just
          "Tokyo: 🌧️ +12°C\n"

        We return the raw string as the user_message — it's already concise.
        """
        text = body.strip()
        if not text:
            return ToolResult(
                success=False,
                error_message="empty response from weather service",
                user_message="couldn't get the weather — try a different location",
            )

        # wttr.in returns "Unknown location" for bad queries
        if "unknown location" in text.lower() or "unknown" in text.lower():
            return ToolResult(
                success=False,
                error_message=f"unknown location: {args.location}",
                user_message=f"couldn't find weather for '{args.location}' — try a city name",
            )

        return ToolResult(
            success=True,
            data={
                "location": args.location,
                "raw": text,
            },
            user_message=text,
        )
