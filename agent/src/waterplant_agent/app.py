"""HTTP surface for the maintenance assistant.

Accepts a question, forwards the caller's Authorization header untouched to the
MCP servers, and returns the answer along with the tool calls it made.

Returning the tool calls is not debug output — it is the point. Participants
need to see what the agent *did*, separately from what it *says* it did, and a
denial has to be attributable to the component that refused it.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Literal

from pydantic import BaseModel, Field

from . import settings
from .runtime import RateLimited, run
from .streaming import run_stream

app = FastAPI(title="Water Plant Maintenance Assistant", version="0.1.0")


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    persona: str = "operator"
    #: The conversation so far, replayed by the caller. The agent holds no
    #: session state — see runtime.opening_messages for why, and for what a
    #: client-supplied transcript does and does not mean.
    history: list[Turn] = Field(default_factory=list, max_length=100)


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


def _history(req: ChatRequest) -> list[dict[str, str]]:
    return [turn.model_dump() for turn in req.history]


@app.post("/chat")
async def chat(req: ChatRequest, request: Request) -> JSONResponse:
    token = request.headers.get("authorization")
    try:
        result = await run(req.message, token=token, history=_history(req))
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


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    """Server-sent events for the same work as /chat.

    /chat is kept for the evaluation harness and for anything that wants a
    single result. The console uses this, because a seven-step tool chain is a
    long time to show nothing — and because watching the tool calls land is the
    point, not a progress bar.
    """
    token = request.headers.get("authorization")

    async def events():
        try:
            async for event in run_stream(req.message, token=token, history=_history(req)):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # never leave the stream hanging open
            yield f'data: {json.dumps({"type": "error", "detail": str(exc)})}\n\n'

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # The OpenShift router buffers by default, which would hold the
            # whole stream back and defeat the point.
            "X-Accel-Buffering": "no",
        },
    )
