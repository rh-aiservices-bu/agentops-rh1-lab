"""Configuration, all via environment.

The LiteLLM variable names match what RHDP provisions into the showroom pod, so
the lab guide and the agent cannot drift apart. The key arrives from a Secret
via envFrom and is never logged, never defaulted, and never written to a chart
value.
"""

from __future__ import annotations

import os

NAMESPACE = os.environ.get("NAMESPACE", "wp-dev-phayes")

LITELLM_API_BASE_URL = os.environ.get(
    "LITELLM_API_BASE_URL", "https://maas-rhdp.apps.maas.redhatworkshops.io/v1"
).rstrip("/")
LITELLM_MODEL = os.environ.get("LITELLM_MODEL", "qwen3-235b")
LITELLM_VIRTUAL_KEY = os.environ.get("LITELLM_VIRTUAL_KEY", "")

TEMPERATURE = float(os.environ.get("AGENT_TEMPERATURE", "0"))
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "14"))
#: How many prior turns of the conversation the console may replay. Caps
#: what an unbounded transcript can do to the model's context window.
MAX_HISTORY_TURNS = int(os.environ.get("AGENT_MAX_HISTORY_TURNS", "20"))
REQUEST_TIMEOUT_S = float(os.environ.get("AGENT_TIMEOUT_S", "180"))

#: The shared MaaS endpoint returns 429 under very little load — a single
#: sequential client saw 52 of them across 15 runs. Backoff absorbs it; it does
#: not solve it. See the reliability spike.
RATE_LIMIT_RETRIES = int(os.environ.get("AGENT_RATE_LIMIT_RETRIES", "5"))

_MCP_BASE = os.environ.get("MCP_BASE", "")


def _url(server: str) -> str:
    if _MCP_BASE:
        return f"{_MCP_BASE.rstrip('/')}/{server}/mcp"
    return f"http://{server}.{NAMESPACE}.svc:8080/mcp"


MCP_SERVERS = {
    "telemetry-mcp": _url("telemetry-mcp"),
    "maintenance-mcp": _url("maintenance-mcp"),
    "control-mcp": _url("control-mcp"),
}
