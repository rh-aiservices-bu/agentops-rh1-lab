"""MCP tool bridge.

Connects to the MCP servers, exposes their tools to the model in OpenAI
function-calling form, and dispatches calls back.

Two decisions here are load-bearing for the lab rather than incidental:

**Tools are listed per request, not cached at startup.** Once MCP Gateway
filters the tool list by the caller's identity, a cached list would be the
wrong list — an Operator would be offered tools their token cannot invoke, and
the model would spend turns being denied. Listing per request costs three fast
HTTP calls and keeps the offered tools honest for whoever is asking.

**Connections are opened per request inside one AsyncExitStack.** Holding MCP
clients open across requests means their anyio cancel scopes get entered and
exited in different tasks, which fails at runtime. The servers are stateless
(`stateless_http=True`), so reconnecting is cheap and correct.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from . import settings


class _AuthedTransport:
    """Transport that carries the caller's bearer token to an MCP server.

    The SDK's Client accepts a URL or a Transport and offers no headers
    argument, but `streamable_http_client` takes an `http_client`. So identity
    is propagated by handing it an httpx client with the Authorization header
    already set, and wrapping that as the async context manager the Transport
    protocol expects.

    The token is attached and never inspected. The agent makes no authorization
    decisions — but it must *propagate* identity, or identity-aware tool
    authorization is impossible downstream, and Scenarios 1, 4 and 6 have
    nothing to enforce against.
    """

    def __init__(self, url: str, token: str | None) -> None:
        self._url = url
        self._token = token
        self._http: httpx.AsyncClient | None = None
        self._cm = None

    async def __aenter__(self):
        headers = {"Authorization": self._token} if self._token else {}
        self._http = httpx.AsyncClient(
            headers=headers, timeout=settings.REQUEST_TIMEOUT_S
        )
        self._cm = streamable_http_client(self._url, http_client=self._http)
        return await self._cm.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._cm is not None:
                return await self._cm.__aexit__(exc_type, exc, tb)
        finally:
            if self._http is not None:
                await self._http.aclose()


@dataclass
class ToolCallRecord:
    """What the agent tried, and what came back. Feeds the trace and the UI."""

    name: str
    server: str
    arguments: dict[str, Any]
    ok: bool
    result: str = ""
    error: str | None = None
    #: Which component refused, when something refused. The lab has participants
    #: debug denials from the trace, so "who said no" has to survive.
    denied_by: str | None = None


@dataclass
class ToolSession:
    clients: dict[str, Client]
    catalog: dict[str, str]
    schemas: list[dict]
    records: list[ToolCallRecord] = field(default_factory=list)

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke a tool and return the text the model should see."""
        server = self.catalog.get(name)
        if server is None:
            record = ToolCallRecord(
                name=name, server="?", arguments=arguments, ok=False,
                error=f"No such tool: {name}",
            )
            self.records.append(record)
            return record.error

        try:
            result = await self.clients[server].call_tool(name, arguments)
            text = "\n".join(b.text for b in result.content)
            # The client is constructed with raise_exceptions=False, so a tool
            # failure arrives as a result flagged is_error rather than as a
            # raised exception. Both paths have to be handled or a denial would
            # be recorded as a success with an error message in the body.
            if getattr(result, "is_error", False):
                raise RuntimeError(text or "tool reported an error")
            self.records.append(
                ToolCallRecord(name, server, arguments, ok=True, result=text)
            )
            return text
        except Exception as exc:
            message = str(exc)
            # Name the refusing component so the agent can report it and the
            # trace records it. An agent that says "something went wrong" gives
            # a participant nothing to debug in module 6.
            denied_by = _attribute_denial(message)
            self.records.append(
                ToolCallRecord(
                    name, server, arguments, ok=False, error=message, denied_by=denied_by
                )
            )
            prefix = f"Denied by {denied_by}: " if denied_by else "Error: "
            return f"{prefix}{message}"


def _attribute_denial(message: str) -> str | None:
    lowered = message.lower()
    if "403" in message or "forbidden" in lowered or "not authorized" in lowered:
        return "MCP Gateway (authorization)"
    if "401" in message or "unauthorized" in lowered:
        return "MCP Gateway (authentication)"
    if "permission denied" in lowered or "operation not permitted" in lowered:
        return "OpenShell execution policy"
    if "connection" in lowered and "refused" in lowered:
        return "network policy"
    return None


@contextlib.asynccontextmanager
async def open_tools(token: str | None):
    """Open every MCP server for one request, carrying the caller's token.

    The token is forwarded untouched and never inspected. The agent makes no
    authorization decisions — that belongs to MCP Gateway. But the agent must
    *propagate* identity, or identity-aware authorization is impossible
    downstream.
    """
    async with contextlib.AsyncExitStack() as stack:
        clients: dict[str, Client] = {}
        catalog: dict[str, str] = {}
        schemas: list[dict] = []

        for name, url in settings.MCP_SERVERS.items():
            client = await stack.enter_async_context(Client(_AuthedTransport(url, token)))
            clients[name] = client
            for tool in (await client.list_tools()).tools:
                catalog[tool.name] = name
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.input_schema,
                        },
                    }
                )

        yield ToolSession(clients=clients, catalog=catalog, schemas=schemas)
