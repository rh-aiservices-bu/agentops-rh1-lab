"""Speaking to an agent that is not ours.

BYOA is the lab's premise, so the console has to survive a harness swap. Our own
agent serves `/chat` and `/chat/stream` in a shape built for this console; Hermes
serves an OpenAI-compatible `/v1/chat/completions` and nothing else. This module
is the whole of the difference, kept in one file so the swap stays a
configuration change (`AGENT_PROTOCOL`) rather than a fork of the console.

Two things it cannot paper over, and deliberately does not pretend to:

**Identity stops here.** Everywhere else in this codebase the caller's token is
forwarded untouched, because authorization belongs to MCP Gateway and the
propagation is what makes Scenarios 1, 4 and 6 possible. Hermes authenticates
with a single static server key and would reject a participant token outright, so
on this path the BFF substitutes a service credential and the caller's identity
does not reach the agent. That is a real reduction in what the lab can
demonstrate, not an implementation detail — see `identity_is_propagated`.

**There is no tool trace.** Hermes runs its agent loop server-side and returns
only the finished answer; the tool calls never cross the wire. The console's
agent-trace panel therefore has nothing to render, which matters because module 6
has participants debug an over-restrictive policy *from the trace*. Rather than
show an empty panel that reads as a broken console, the stream opens with a
status line saying so. The durable fix is tracing at the gateway, where it
survives a harness swap; this module only has to be honest until that exists.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

#: "waterplant" — our own agent, native event shape, caller's token forwarded.
#: "openai" — any OpenAI-compatible server, currently Hermes.
PROTOCOL = os.environ.get("AGENT_PROTOCOL", "waterplant").strip().lower()
#: Static server credential for the openai protocol. From a Secret, never a
#: chart value. Unused by the waterplant protocol, which forwards the caller's.
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "")
#: The `model` field an OpenAI-compatible server expects. Hermes ignores the
#: value beyond requiring one, but a wrong name is a confusing 4xx.
AGENT_MODEL = os.environ.get("AGENT_MODEL", "hermes-agent")

_NO_TRACE = (
    "harness does not expose tool calls — plant readings below are the "
    "ground truth for what it actually did"
)


def identity_is_propagated() -> bool:
    """Does the caller's token reach the agent on this path?

    Surfaced through /api/config so the console can be truthful about it, and so
    a participant is never shown an identity exercise that silently cannot work.
    """
    return PROTOCOL == "waterplant"


def headers_for(caller_auth: str | None, *, sse: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {"accept": "text/event-stream"} if sse else {}
    if PROTOCOL == "openai":
        # The caller's token is dropped, not forwarded alongside: sending both
        # would imply an identity reaches the agent when none does.
        if AGENT_API_KEY:
            headers["authorization"] = f"Bearer {AGENT_API_KEY}"
    elif caller_auth:
        headers["authorization"] = caller_auth
    return headers


def request(url: str, message: str, persona: str, *, stream: bool) -> tuple[str, dict]:
    """The (path, body) this harness expects for one turn."""
    if PROTOCOL == "openai":
        body: dict[str, Any] = {
            "model": AGENT_MODEL,
            "messages": [{"role": "user", "content": message}],
        }
        if stream:
            body["stream"] = True
        return f"{url}/v1/chat/completions", body
    path = f"{url}/chat/stream" if stream else f"{url}/chat"
    return path, {"message": message, "persona": persona}


def translate_reply(body: dict) -> dict:
    """An OpenAI completion in the shape /api/chat already returns."""
    if PROTOCOL != "openai":
        return body
    try:
        reply = body["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        # Pass an unrecognised body through rather than flattening it to an
        # empty reply — a server error is more useful than a blank answer.
        return body
    return {
        "reply": reply,
        "steps": 0,
        "rateLimitRetries": 0,
        "toolCalls": [],
        "traceAvailable": False,
    }


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


async def relay_stream(
    client: httpx.AsyncClient,
    url: str,
    message: str,
    persona: str,
    caller_auth: str | None,
) -> AsyncIterator[bytes]:
    """One turn as this console's server-sent events, whatever the harness is."""
    target, body = request(url, message, persona, stream=True)
    headers = headers_for(caller_auth, sse=True)

    if PROTOCOL != "openai":
        # Native shape: the agent already emits these events, so relay raw and
        # add nothing. The BFF decides nothing here, exactly as elsewhere.
        try:
            async with client.stream("POST", target, json=body, headers=headers) as up:
                async for chunk in up.aiter_raw():
                    yield chunk
        except httpx.HTTPError as exc:
            yield _sse({"type": "error", "detail": f"agent unreachable: {exc}"})
        return

    yield _sse({"type": "status", "message": _NO_TRACE})
    try:
        async with client.stream("POST", target, json=body, headers=headers) as up:
            if up.status_code >= 400:
                detail = (await up.aread()).decode(errors="replace")[:400]
                yield _sse(
                    {"type": "error", "detail": f"agent returned HTTP {up.status_code}: {detail}"}
                )
                return

            # A server that ignores `stream: true` answers with one JSON body.
            # Hermes has done both across versions, so branch on what arrived
            # rather than on what was asked for.
            if "text/event-stream" not in up.headers.get("content-type", ""):
                raw = (await up.aread()).decode(errors="replace")
                try:
                    reply = json.loads(raw)["choices"][0]["message"]["content"] or ""
                except (ValueError, KeyError, IndexError, TypeError):
                    yield _sse({"type": "error", "detail": f"unparseable reply: {raw[:400]}"})
                    return
                yield _sse({"type": "token", "text": reply})
                yield _sse({"type": "done", "steps": 0, "rateLimitRetries": 0})
                return

            async for line in up.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                try:
                    delta = chunk["choices"][0]["delta"]
                except (KeyError, IndexError, TypeError):
                    continue
                if text := delta.get("content"):
                    yield _sse({"type": "token", "text": text})
                # Not expected from Hermes, which loops server-side — but a
                # different OpenAI-compatible harness may forward them, and if
                # one does the trace panel should light up for free.
                for call in delta.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    if not fn.get("name"):
                        continue
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    yield _sse(
                        {
                            "type": "tool_call",
                            "name": fn["name"],
                            "server": "unknown",
                            "arguments": args,
                        }
                    )
    except httpx.HTTPError as exc:
        yield _sse({"type": "error", "detail": f"agent unreachable: {exc}"})
        return

    yield _sse({"type": "done", "steps": 0, "rateLimitRetries": 0})
