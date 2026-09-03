"""Water Plant UI — backend for frontend.

Serves the operator console and proxies to plant-api and (once it exists) the
agent. It holds no state and makes no authorization decisions: the caller's
token is forwarded untouched, exactly as the agent does, because authorization
belongs to MCP Gateway (implementation-plan.md §D8).

The dashboard deliberately reads plant-api directly rather than going through
the agent. Two reasons: the plant must stay observable when the agent is
broken, denied or not yet deployed; and a participant needs to *see* the
consequence of a tool call independently of what the agent claims happened.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

PLANT_API_URL = os.environ.get("PLANT_API_URL", "http://plant-api:8080")
# Empty until the agent is deployed — the chat pane degrades to a clear
# "not yet available" state rather than failing opaquely.
AGENT_URL = os.environ.get("AGENT_URL", "").rstrip("/")
MLFLOW_URL = os.environ.get("MLFLOW_URL", "")
TIMEOUT_S = float(os.environ.get("HTTP_TIMEOUT_S", "30"))

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Water Plant UI", version="0.1.0")
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT_S)
    return _client


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
async def config() -> dict[str, Any]:
    """What the front end needs to know about its environment."""
    return {
        "agentAvailable": bool(AGENT_URL),
        "mlflowUrl": MLFLOW_URL,
        "namespace": os.environ.get("NAMESPACE", ""),
    }


@app.get("/api/state")
async def state() -> JSONResponse:
    """Plant state and safety readout in one call, for the dashboard poll."""
    try:
        plant, safety = await _gather("/state", "/safety")
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"error": f"plant-api unreachable: {exc}"}, status_code=503
        )
    return JSONResponse({"plant": plant, "safety": safety})


async def _gather(*paths: str) -> list[Any]:
    out = []
    for path in paths:
        response = await client().get(f"{PLANT_API_URL}{path}")
        response.raise_for_status()
        out.append(response.json())
    return out


class ChatRequest(BaseModel):
    message: str
    persona: str = "operator"


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> JSONResponse:
    """Forward a question to the agent, carrying the caller's identity.

    The Authorization header is passed straight through and never inspected.
    That propagation is what makes identity-aware tool authorization possible
    later — without it there is no Scenario 1, 4 or 6 — but the decision itself
    is always somebody else's.
    """
    if not AGENT_URL:
        return JSONResponse(
            {
                "error": "agent_not_deployed",
                "detail": (
                    "The maintenance agent is not deployed in this environment "
                    "yet. The plant dashboard is live and the MCP servers are "
                    "running — you can still watch the plant respond to direct "
                    "control actions."
                ),
            },
            status_code=503,
        )

    headers = {}
    if auth := request.headers.get("authorization"):
        headers["authorization"] = auth

    try:
        response = await client().post(
            f"{AGENT_URL}/chat",
            json={"message": req.message, "persona": req.persona},
            headers=headers,
        )
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"agent unreachable: {exc}"}, status_code=502)

    return JSONResponse(response.json(), status_code=response.status_code)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text())


app.mount("/static", StaticFiles(directory=STATIC), name="static")
