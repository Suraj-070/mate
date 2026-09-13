"""Base class for HTTP-based tools (Phase 4).

Extends BaseTool with whitelisted HTTP behavior:
  - Allowed hosts defined as a class attribute (NOT configurable at runtime —
    prevents the LLM from discovering new endpoints)
  - Per-request timeout
  - Response size limit (truncated beyond it)
  - Only HTTPS allowed by default (override per-tool if needed)
  - User-Agent set to "ai-groupmate/0.1"

Subclasses implement:
  - host: the exact hostname allowed (e.g. "wttr.in")
  - build_url(args) -> str: construct the URL from validated args
  - parse_response(body) -> ToolResult: transform the HTTP body into a ToolResult

The framework handles all HTTP I/O, error handling, and size limits. The
subclass only decides URL construction + response parsing.
"""
from __future__ import annotations

import abc
from typing import Any, Optional

import httpx
from pydantic import BaseModel

from app.config.settings import get_settings
from app.tools.registry import BaseTool
from app.tools.schemas import ToolContext, ToolResult
from app.utils.logging import get_logger

log = get_logger(__name__)


class BaseHTTPTool(BaseTool, abc.ABC):
    """Abstract base for HTTP-based tools.

    Subclasses MUST define:
      - host: hostname (string) — only this host is allowed
      - build_url(args) -> str: path + query string

    Subclasses MAY override:
      - allowed_methods: set of HTTP methods (default {"GET"})
      - require_https: bool (default True)
      - parse_response(body: str, args) -> ToolResult

    The framework handles:
      - Validating the URL matches the allowed host
      - Sending the request with the configured timeout
      - Truncating the response body
      - Wrapping all errors into ToolResult(success=False, ...)
    """

    host: str = ""
    allowed_methods: set[str] = {"GET"}
    require_https: bool = True

    @abc.abstractmethod
    def build_url(self, args: BaseModel) -> str:
        """Build the path + query string. The framework prepends the scheme + host.

        e.g. return "/Sydney?format=3" → final URL "https://wttr.in/Sydney?format=3"
        """
        ...

    @abc.abstractmethod
    def parse_response(self, body: str, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Transform the HTTP response body into a ToolResult.

        Called only on 2xx responses. The framework handles non-2xx as errors.
        """
        ...

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Final execute — handles HTTP I/O. Do NOT override in subclasses."""
        if not self.host:
            return ToolResult(
                success=False,
                error_message="tool misconfigured: no host defined",
                user_message="this tool isn't configured correctly",
            )

        # Build URL
        try:
            path_and_query = self.build_url(args)
        except Exception as e:
            log.exception("http_tool_url_build_failed", tool=self.name, error=str(e))
            return ToolResult(
                success=False,
                error_message=f"failed to build URL: {e}",
                user_message="couldn't construct the request",
            )

        scheme = "https" if self.require_https else "http"
        url = f"{scheme}://{self.host}{path_and_query}"

        # Defensive check: even though we constructed the URL ourselves,
        # make sure the host hasn't been tampered with (e.g. via path injection)
        # by parsing the final URL.
        try:
            import urllib.parse
            parsed = urllib.parse.urlparse(url)
            if parsed.hostname != self.host:
                log.error(
                    "http_tool_host_mismatch",
                    tool=self.name,
                    expected=self.host,
                    actual=parsed.hostname,
                )
                return ToolResult(
                    success=False,
                    error_message="host validation failed",
                    user_message="couldn't make that request",
                )
        except Exception as e:
            return ToolResult(
                success=False,
                error_message=f"URL parse error: {e}",
                user_message="couldn't make that request",
            )

        # Fetch
        settings = get_settings()
        timeout = settings.http_tool_timeout_seconds
        max_bytes = settings.http_tool_max_response_bytes

        headers = {
            "User-Agent": "ai-groupmate/0.1",
            "Accept": "text/plain, application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.request(
                    method="GET",  # we only support GET for now
                    url=url,
                    headers=headers,
                )
        except httpx.TimeoutException:
            log.warning("http_tool_timeout", tool=self.name, url=url, timeout=timeout)
            return ToolResult(
                success=False,
                error_message=f"request timed out after {timeout}s",
                user_message="the service took too long to respond — try again in a bit",
            )
        except httpx.RequestError as e:
            log.warning("http_tool_request_error", tool=self.name, url=url, error=str(e))
            return ToolResult(
                success=False,
                error_message=f"request failed: {e}",
                user_message="couldn't reach the service",
            )

        if response.status_code >= 400:
            log.warning(
                "http_tool_http_error",
                tool=self.name,
                url=url,
                status=response.status_code,
            )
            return ToolResult(
                success=False,
                error_message=f"HTTP {response.status_code}",
                user_message=f"the service returned an error ({response.status_code})",
            )

        # Truncate body if needed
        body = response.text
        if len(body.encode("utf-8")) > max_bytes:
            body = body.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            log.info("http_tool_response_truncated", tool=self.name, max_bytes=max_bytes)

        # Delegate parsing to subclass
        try:
            return self.parse_response(body, args, ctx)
        except Exception as e:
            log.exception("http_tool_parse_failed", tool=self.name, error=str(e))
            return ToolResult(
                success=False,
                error_message=f"failed to parse response: {e}",
                user_message="got a response but couldn't make sense of it",
            )
