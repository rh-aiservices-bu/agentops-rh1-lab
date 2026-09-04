"""HTTP surface for the maintenance assistant.

Accepts a question, forwards the caller's Authorization header untouched to the
MCP servers, and returns the answer along with the tool calls it made.

Returning the tool calls is not debug output — it is the point. Participants
need to see what the agent *did*, separately from what it *says* it did, and a
denial has to be attributable to the component that refused it.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import settings
from .runtime import RateLimited, run

app = FastAPI(title="Water Plant Maintenance Assistant", version="0.1.0")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    persona: str = "operator"


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/info")
async def info() -> dict:
    return {
        "model": settings.LITELLM_MODEL,
        "mcpServers": list(settings.MCP_SERVERS),
        "keyConfigured": bool(settings.LITELLM_VIRTUAL_KEY),
    }


@app.post("/chat")
async def chat(req: ChatRequest, request: Request) -> JSONResponse:
    token = request.headers.get("authorization")
    try:
        result = await run(req.message, token=token)
    except RateLimited as exc:
        return JSONResponse({"error": "rate_limited", "detail": str(exc)}, status_code=429)

    return JSONResponse(
        {
            "reply": result.reply,
            "steps": result.steps,
            "rateLimitRetries": result.rate_limit_retries,
            "toolCalls": [
                {
                    "name": r.name,
                    "server": r.server,
                    "arguments": r.arguments,
                    "ok": r.ok,
                    "deniedBy": r.denied_by,
                    "error": r.error,
                }
                for r in result.tool_calls
            ],
        }
    )
