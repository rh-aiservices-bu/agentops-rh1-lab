"""Thin HTTP client for plant-api.

The MCP servers hold no state of their own (implementation-plan.md §2.8); they
translate MCP tool calls into plant-api requests and hand the results back. That
is deliberate — a stateless MCP server can be restarted or rescheduled without
an attendee noticing, and MCP Gateway can route to it without session affinity.

Note what this client does *not* do: it does not inspect, validate or authorize
anything. Authorization belongs to MCP Gateway. Putting a check here would move
enforcement into the application and defeat the point of the lab (§D8).
"""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import settings


class PlantAPIError(ToolError):
    """Raised when plant-api rejects or fails a request.

    Subclassing ToolError is load-bearing, not tidiness. The SDK treats a
    ToolError as a deliberate, reportable failure and passes its message
    through to the caller; any other exception is masked behind a generic
    "Error executing tool <name>" with the detail discarded.

    The lab depends on the detail surviving. Section 6 has attendees debug an
    over-restrictive policy from the MLflow trace, which only works if the
    trace records *what* refused the call and why. An agent that receives
    "Error executing tool" has nothing to report and will tend to guess.
    """


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.PLANT_API_URL,
            timeout=settings.REQUEST_TIMEOUT_S,
        )
    return _client


async def request(method: str, path: str, **kwargs: Any) -> Any:
    try:
        response = await _get_client().request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise PlantAPIError(f"plant-api unreachable at {path}: {exc}") from exc

    if response.is_error:
        detail = _detail(response)
        raise PlantAPIError(f"plant-api returned {response.status_code} for {path}: {detail}")
    return response.json()


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:400]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])[:400]
    return str(body)[:400]


async def get(path: str, **kwargs: Any) -> Any:
    return await request("GET", path, **kwargs)


async def post(path: str, **kwargs: Any) -> Any:
    return await request("POST", path, **kwargs)


async def patch(path: str, **kwargs: Any) -> Any:
    return await request("PATCH", path, **kwargs)


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
