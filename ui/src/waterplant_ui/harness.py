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

**There is no tool trace in the OpenAI shape.** Hermes runs its agent loop
server-side and the completion carries only the finished answer. (Its stream
does add a non-standard `event: hermes.tool.progress` frame per tool call, seen
on cluster-z5jhc; this module does not read it yet.) The console's agent-trace
panel therefore has nothing to render, which matters
because module 6 has participants debug an over-restrictive policy *from the
trace*. Rather than show an empty panel that reads as a broken console, the
stream opens with a status line saying so.

They are not lost, though — Hermes's `hermes_otel` plugin exports them to MLflow
as `TOOL` spans. So the turn ends with a link to its *own* trace, resolved by
`traces.py` after the answer, and falls back to MLflow's front door when the
lookup cannot pin one. That is still a redirect rather than a fix: the
participant leaves the console to read it. Rendering those spans in the panel is
the next step, and tracing at the gateway — where it survives a harness swap —
is the durable one.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from . import traces

#: "waterplant" — our own agent, native event shape, caller's token forwarded.
#: "openai" — any OpenAI-compatible server, currently Hermes.
PROTOCOL = os.environ.get("AGENT_PROTOCOL", "waterplant").strip().lower()
#: Static server credential for the openai protocol. From a Secret, never a
#: chart value. Unused by the waterplant protocol, which forwards the caller's.
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "")
#: The `model` field an OpenAI-compatible server expects. Hermes ignores the
#: value beyond requiring one, but a wrong name is a confusing 4xx.
AGENT_MODEL = os.environ.get("AGENT_MODEL", "hermes-agent")
#: How many prior turns the console may replay. An unbounded transcript
#: eventually walks into the model's context limit mid-workshop. Not applied
#: when Hermes keeps the session: it trims its own transcript.
MAX_HISTORY_TURNS = int(os.environ.get("AGENT_MAX_HISTORY_TURNS", "20"))
#: Hermes loads the conversation from its own store when a request names it
#: here, instead of taking history from the body. See `request` for why that
#: matters. The store is the sandbox's state.db, so when the bridge recreates
#: the sandbox an ongoing conversation silently starts over.
SESSION_HEADER = "X-Hermes-Session-Id"

#: Only for a deployment with no trace lookup wired. Where the lookup *is*
#: configured the turn ends with a link to its own trace, and opening with an
#: explanation of an absence the participant is about to be handed the fix for
#: is noise — so the line is not emitted at all.
_NO_TRACE = (
    "harness does not expose tool calls over the wire — plant readings below "
    "are the ground truth for what it actually did"
)
#: Deliberately hedged. The harness on this path is Hermes today, whose
#: `hermes_otel` plugin does export TOOL spans, but the protocol is "any
#: OpenAI-compatible server" and a different one may trace nothing.
_NO_TRACE_LINK = "look for this turn's tool calls in MLflow →"
#: Shown when the turn produced no trace we can point at. It used to read
#: "this turn's trace has not been indexed yet" and offer MLflow's front door,
#: which said nothing useful and sent the participant to a page they then had
#: to search. Either there is a trace to open or there is not.
#: The link says everything. "this turn was traced, see the trace" spent a
#: line restating what the link itself makes obvious.
_NOT_INDEXED = "no trace was collected for this turn"
_TRACE_FOUND = ""
_TRACE_FOUND_LINK = "see the trace →"


def _no_trace_event() -> dict | None:
    if traces.enabled():
        return None
    event: dict[str, Any] = {"type": "status", "message": _NO_TRACE}
    # The participant's own experiment list, not MLflow's front door: the
    # status line is only a dead end without somewhere useful to send them.
    # Unset (no tracing wired at all) drops the link rather than offering a
    # broken one.
    if landing := traces.landing_url():
        event["link"] = {"href": landing, "text": _NO_TRACE_LINK}
    return event


async def _trace_link_event(message: str, started_ms: int) -> dict | None:
    """The turn's own trace, once the exporter has written it.

    Emitted after the answer rather than before it because that is when the
    trace exists: the plugin flushes at the end of the turn. Nothing is emitted
    if it cannot be pinned to this exact turn — see `traces.find_trace`.
    """
    if not traces.enabled():
        return None
    found = await traces.find_trace(message, started_ms)
    if not found:
        # Say so plainly and offer nothing. A link to MLflow's front door is
        # not a link to this turn, and dressing it up as one sends people
        # hunting through a trace list for something that may not be there.
        # Marked as a fault rather than a remark: tracing is wired up on this
        # deployment, so a turn arriving without one is something being wrong,
        # not an expected absence.
        return {"type": "status", "level": "bad", "message": _NOT_INDEXED}
    trace_id, experiment_id = found
    return {
        "type": "status",
        "message": _TRACE_FOUND,
        "link": {"href": traces.trace_url(trace_id, experiment_id), "text": _TRACE_FOUND_LINK},
    }


def identity_is_propagated() -> bool:
    """Does the caller's token reach the agent on this path?

    Surfaced through /api/config so the console can be truthful about it, and so
    a participant is never shown an identity exercise that silently cannot work.
    """
    return PROTOCOL == "waterplant"


def headers_for(
    caller_auth: str | None, *, sse: bool = False, session_id: str | None = None
) -> dict[str, str]:
    headers: dict[str, str] = {"accept": "text/event-stream"} if sse else {}
    if PROTOCOL == "openai":
        # The caller's token is dropped, not forwarded alongside: sending both
        # would imply an identity reaches the agent when none does.
        if AGENT_API_KEY:
            headers["authorization"] = f"Bearer {AGENT_API_KEY}"
        # Hermes honours this only on an authenticated request, which is why
        # it lives beside the server key.
        if session_id:
            headers[SESSION_HEADER] = session_id
    elif caller_auth:
        headers["authorization"] = caller_auth
    return headers


def request(
    url: str,
    message: str,
    persona: str,
    history: list[dict[str, str]] | None = None,
    *,
    stream: bool,
    session_id: str | None = None,
) -> tuple[str, dict]:
    """The (path, body) this harness expects for one turn.

    A follow-up needs the conversation behind it: "check pump 3, make sure it's
    running" is answered and offers to start it, then "yes" arrives cold and
    gets "Hello! How can I assist you today?" Where that conversation comes
    from differs per protocol, which is the reason this is a function and not a
    template.

    Our own agent keeps no session, so the console's transcript travels beside
    the question.

    Hermes keeps one, and with a `session_id` it must be left to: only the new
    message is sent, and Hermes reloads the real transcript, tool calls and
    results included. Replaying the console's transcript instead looks
    equivalent and is not. In stateless mode Hermes keeps only role and content
    from body history (gateway/platforms/api_server.py, chat completions), so
    the model is shown earlier answers full of plant readings with no tool call
    behind them, and copies that: it narrates the action, invents the result
    and calls nothing. Measured on cluster-z5jhc, same two read-only turns, 4
    runs each: the second turn called its tool 1/4 times replayed, 4/4 with the
    session. Without a session id the replay remains, as the only option a
    generic OpenAI server offers.
    """
    turns = [
        {"role": t["role"], "content": t["content"]}
        for t in (history or [])[-MAX_HISTORY_TURNS:]
        if t.get("role") in ("user", "assistant") and t.get("content")
    ]
    if PROTOCOL == "openai":
        prior = [] if session_id else turns
        body: dict[str, Any] = {
            "model": AGENT_MODEL,
            "messages": [*prior, {"role": "user", "content": message}],
        }
        if stream:
            body["stream"] = True
        return f"{url}/v1/chat/completions", body
    path = f"{url}/chat/stream" if stream else f"{url}/chat"
    return path, {"message": message, "persona": persona, "history": turns}


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


def sse(event: dict) -> bytes:
    """One console SSE frame. Public because `app.py` and `guardrails.py`
    emit frames of their own — a blocked prompt never reaches this module."""
    return f"data: {json.dumps(event)}\n\n".encode()


async def relay_stream(
    client: httpx.AsyncClient,
    url: str,
    message: str,
    persona: str,
    history: list[dict[str, str]] | None,
    caller_auth: str | None,
    session_id: str | None = None,
) -> AsyncIterator[bytes]:
    """One turn as this console's server-sent events, whatever the harness is."""
    target, body = request(url, message, persona, history, stream=True, session_id=session_id)
    headers = headers_for(caller_auth, sse=True, session_id=session_id)

    if PROTOCOL != "openai":
        # Native shape: the agent already emits these events, so relay raw and
        # add nothing. The BFF decides nothing here, exactly as elsewhere.
        try:
            async with client.stream("POST", target, json=body, headers=headers) as up:
                async for chunk in up.aiter_raw():
                    yield chunk
        except httpx.HTTPError as exc:
            yield sse({"type": "error", "detail": f"agent unreachable: {exc}"})
        return

    # Before the request, not after the answer: the trace is stamped with when
    # the turn began, and a window opened afterwards can miss its own trace.
    started_ms = int(time.time() * 1000)
    if opening := _no_trace_event():
        yield sse(opening)
    try:
        async with client.stream("POST", target, json=body, headers=headers) as up:
            if up.status_code >= 400:
                detail = (await up.aread()).decode(errors="replace")[:400]
                yield sse(
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
                    yield sse({"type": "error", "detail": f"unparseable reply: {raw[:400]}"})
                    return
                yield sse({"type": "token", "text": reply})
                if link := await _trace_link_event(message, started_ms):
                    yield sse(link)
                yield sse({"type": "done", "steps": 0, "rateLimitRetries": 0})
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
                    yield sse({"type": "token", "text": text})
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
                    yield sse(
                        {
                            "type": "tool_call",
                            "name": fn["name"],
                            "server": "unknown",
                            "arguments": args,
                        }
                    )
    except httpx.HTTPError as exc:
        yield sse({"type": "error", "detail": f"agent unreachable: {exc}"})
        return

    if link := await _trace_link_event(message, started_ms):
        yield sse(link)
    yield sse({"type": "done", "steps": 0, "rateLimitRetries": 0})
