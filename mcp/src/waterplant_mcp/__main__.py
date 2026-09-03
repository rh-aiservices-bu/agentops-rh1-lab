"""Entry point. One image, three servers, selected by argument or environment.

    python -m waterplant_mcp telemetry

Each runs streamable HTTP on /mcp — never stdio. MCP Gateway can only route,
authorize and token-exchange over HTTP, so stdio would make Scenarios 1, 3, 4
and 6 undemonstrable (implementation-plan.md §D3).
"""

from __future__ import annotations

import os
import sys

from mcp.server.transport_security import TransportSecuritySettings

from . import settings

SERVERS = ("telemetry", "maintenance", "control")


def _load(name: str):
    if name == "telemetry":
        from .telemetry import server
    elif name == "maintenance":
        from .maintenance import server
    elif name == "control":
        from .control import server
    else:
        raise SystemExit(f"unknown server {name!r}; expected one of {', '.join(SERVERS)}")
    return server


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MCP_SERVER", "")
    if not name:
        raise SystemExit(f"usage: python -m waterplant_mcp <{'|'.join(SERVERS)}>")

    server = _load(name)
    server.run(
        transport="streamable-http",
        host=settings.HOST,
        port=settings.PORT,
        # Stateless: these servers hold nothing between calls (§2.8), so the
        # gateway can route to any replica without session affinity.
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection="*" not in settings.ALLOWED_HOSTS,
            allowed_hosts=settings.ALLOWED_HOSTS,
            allowed_origins=settings.ALLOWED_HOSTS,
        ),
    )


if __name__ == "__main__":
    main()
