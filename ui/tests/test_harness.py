"""The harness adapter — both protocols, against a stub OpenAI-compatible server.

Worth testing rather than eyeballing for two reasons. It is a parsing layer, and
parsing layers fail on the shapes nobody pictured: a server that ignores
`stream: true`, an upstream 401, an error body that must not be flattened into a
blank reply. And the native path has to stay bit-for-bit unchanged while the
foreign one is added — a regression there is invisible until a participant's
token silently stops reaching the agent, and this repository has already had one
fix reverted by a clean merge with nothing to warn about it.
"""

from __future__ import annotations

import importlib
import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from waterplant_ui import harness as _harness

REPLY = "Pump 4 is at 8.2 mm/s."


def reload_with(monkeypatch, **env):
    """Module-level config is read at import, so re-import per protocol."""
    for key in ("AGENT_PROTOCOL", "AGENT_API_KEY", "AGENT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(_harness)


@pytest.fixture
def openai(monkeypatch):
    return reload_with(
        monkeypatch,
        AGENT_PROTOCOL="openai",
        AGENT_API_KEY="server-key",
        AGENT_MODEL="hermes-agent",
    )


@pytest.fixture
def native(monkeypatch):
    return reload_with(monkeypatch, AGENT_PROTOCOL="waterplant")


def stub(handler) -> httpx.AsyncClient:
    app = Starlette(routes=[Route("/v1/chat/completions", handler, methods=["POST"])])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://stub")


async def _streams(request):
    body = await request.json()
    if not body.get("stream"):
        return JSONResponse({"choices": [{"message": {"content": REPLY}}]})

    async def gen():
        for piece in ("Pump 4 ", "is at ", "8.2 mm/s."):
            yield f'data: {json.dumps({"choices": [{"delta": {"content": piece}}]})}\n\n'.encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _ignores_stream(request):
    return JSONResponse({"choices": [{"message": {"content": "one shot"}}]})


async def _unauthorized(request):
    return JSONResponse({"detail": "invalid API key"}, status_code=401)


async def events(mod, client) -> list[dict]:
    raw = b"".join(
        [c async for c in mod.relay_stream(client, "http://stub", "why?", "operator", "Bearer caller")]
    ).decode()
    return [
        json.loads(frame[5:].strip())
        for frame in raw.split("\n\n")
        if frame.strip().startswith("data:")
    ]


# --- openai protocol -------------------------------------------------------


def test_request_uses_openai_shape(openai):
    target, payload = openai.request("http://hermes-agent:8787", "hi", "operator", stream=False)
    assert target == "http://hermes-agent:8787/v1/chat/completions"
    assert payload == {"model": "hermes-agent", "messages": [{"role": "user", "content": "hi"}]}


def test_caller_token_is_dropped_for_a_service_credential(openai):
    # Not forwarded alongside: sending both would imply an identity reaches the
    # agent when none does.
    assert openai.headers_for("Bearer caller") == {"authorization": "Bearer server-key"}
    assert openai.identity_is_propagated() is False


def test_completion_becomes_the_console_reply_shape(openai):
    body = {"choices": [{"message": {"role": "assistant", "content": REPLY}}]}
    assert openai.translate_reply(body) == {
        "reply": REPLY,
        "steps": 0,
        "rateLimitRetries": 0,
        "toolCalls": [],
        "traceAvailable": False,
    }


def test_unrecognised_body_is_passed_through(openai):
    """A server error is more useful to a participant than a blank answer."""
    body = {"error": "rate_limited", "detail": "429 from the shared endpoint"}
    assert openai.translate_reply(body) == body


@pytest.mark.asyncio
async def test_stream_translates_to_console_events(openai):
    async with stub(_streams) as client:
        evs = await events(openai, client)
    assert [e["type"] for e in evs] == ["status", "token", "token", "token", "done"]
    # The opening status is what stops an empty trace panel reading as a broken
    # console: Hermes loops server-side, so the tool calls never cross the wire.
    assert "does not expose tool calls" in evs[0]["message"]
    assert "".join(e["text"] for e in evs if e["type"] == "token") == REPLY


@pytest.mark.asyncio
async def test_server_that_ignores_stream_still_answers(openai):
    async with stub(_ignores_stream) as client:
        evs = await events(openai, client)
    assert [e["type"] for e in evs] == ["status", "token", "done"]
    assert evs[1]["text"] == "one shot"


@pytest.mark.asyncio
async def test_upstream_rejection_surfaces_as_an_error(openai):
    async with stub(_unauthorized) as client:
        evs = await events(openai, client)
    assert evs[-1]["type"] == "error"
    assert "401" in evs[-1]["detail"] and "invalid API key" in evs[-1]["detail"]


# --- native protocol -------------------------------------------------------


def test_native_path_is_unchanged(native):
    target, payload = native.request("http://waterplant-agent:8080", "hi", "engineer", stream=True)
    assert target == "http://waterplant-agent:8080/chat/stream"
    assert payload == {"message": "hi", "persona": "engineer"}
    assert native.request("http://a", "hi", "operator", stream=False)[0] == "http://a/chat"


def test_native_path_forwards_the_caller_token(native):
    """Without this propagation there is no Scenario 1, 4 or 6."""
    assert native.headers_for("Bearer caller", sse=True) == {
        "accept": "text/event-stream",
        "authorization": "Bearer caller",
    }
    assert native.identity_is_propagated() is True


def test_native_replies_are_not_rewritten(native):
    body = {"reply": REPLY, "steps": 7, "toolCalls": [{"name": "get_pump_status"}]}
    assert native.translate_reply(body) == body
