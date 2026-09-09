"""Entry point for control-mcp.

    python -m control_mcp

Runs the control MCP server over streamable HTTP on /mcp. Never stdio —
MCP Gateway can only route and authorize over HTTP (implementation-plan.md §D3).
"""

from __future__ import annotations

from mcp.server.transport_security import TransportSecuritySettings

from . import settings
from .control import server


def main() -> None:
    server.run(
        transport="streamable-http",
        host=settings.HOST,
        port=settings.PORT,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection="*" not in settings.ALLOWED_HOSTS,
            allowed_hosts=settings.ALLOWED_HOSTS,
            allowed_origins=settings.ALLOWED_HOSTS,
        ),
    )


if __name__ == "__main__":
    main()
