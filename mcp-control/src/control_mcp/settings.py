"""Runtime configuration, all via environment — nothing is baked into images."""

from __future__ import annotations

import os

PLANT_API_URL = os.environ.get("PLANT_API_URL", "http://plant-api:8080")
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8080"))
REQUEST_TIMEOUT_S = float(os.environ.get("PLANT_API_TIMEOUT_S", "10"))

#: The SDK enables DNS-rebinding protection by default and rejects requests
#: whose Host header is not on this list. In-cluster the Host header is a
#: Service name or whatever MCP Gateway forwards, so a restrictive default
#: would fail every call with a confusing 400 and burn lab time on a vector
#: nobody is attacking. The real boundary here is MCP Gateway plus
#: NetworkPolicy, not a Host header check — so this defaults open and is
#: tightened per-deployment if we ever need it.
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "*").split(",") if h.strip()
]
