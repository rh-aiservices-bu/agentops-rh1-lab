"""Streaming agent loop.

Emits events as the agent works rather than after it finishes. Two reasons,
and the second matters more than the first:

* A full investigation is a seven-step tool chain that can take minutes once
  the shared model endpoint starts rate limiting. Silence for that long reads
  as a hang.
* Participants need to see what the agent *did*, live and separately from what
  it later *says* it did. A tool call appearing on screen the moment it fires
  is the difference between "the agent claims it derated the pump" and
  "I watched it call set_pump_speed(4, 70)".

Event shapes, all delivered as SSE `data:` lines:

    {"type": "status",      "message": str}
    {"type": "tool_call",   "name": str, "server": str, "arguments": dict}
    {"type": "tool_result", "name": str, "ok": bool, "deniedBy": str|None,
                            "summary": str}
    {"type": "token",       "text": str}
    {"type": "done",        "steps": int, "rateLimitRetries": int}
    {"type": "error",       "detail": str}
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, AsyncIterator

import httpx

from . import settings
from .runtime import SYSTEM_PROMPT, RateLimited
from .tools import open_tools


async def _stream_completion(
    http: httpx.AsyncClient, payload: dict, retries: list[int]
) -> AsyncIterator[dict]:
    """One streamed completion, retrying through the endpoint's 429s.

    Yields {"content": str} for text deltas and {"tool_delta": ...} fragments.
    Tool calls arrive split across many chunks and keyed by index, so they have
    to be reassembled by the caller rather than used as they land.
    """
    body = dict(payload, stream=True)

    for attempt in range(settings.RATE_LIMIT_RETRIES + 1):
        async with http.stream(
            "POST",
            f"{settings.LITELLM_API_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {settings.LITELLM_VIRTUAL_KEY}"},
            json=body,
        ) as response:
            if response.status_code == 429:
                await response.aread()
                retries[0] += 1
                if attempt == settings.RATE_LIMIT_RETRIES:
                    raise RateLimited(
                        "The model endpoint is rate limited and did not recover "
                        f"after {settings.RATE_LIMIT_RETRIES} retries. This is "
                        "shared capacity, not a fault in the plant or the agent."
                    )
                await asyncio.sleep(min(2**attempt + random.random(), 30))
                continue

            if response.status_code != 200:
                detail = (await response.aread()).decode()[:300]
                raise RuntimeError(f"model endpoint returned {response.status_code}: {detail}")

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                if delta.get("content"):
                    yield {"content": delta["content"]}
                for fragment in delta.get("tool_calls") or []:
                    yield {"tool_delta": fragment}
            return

    raise RateLimited("unreachable")


def _assemble(fragments: list[dict], step: int) -> list[dict]:
    """Rebuild whole tool calls from streamed fragments.

    Each fragment carries an `index`; the name arrives once and the arguments
    arrive as string pieces that concatenate into JSON.

    The id needs care. Providers do not all send one in the delta stream, and a
    tool_call with a null id is rejected on the *next* request — after the tool
    has already run, so it looks like a mid-conversation failure rather than a
    malformed message. Synthesise a stable id when none arrives; it only has to
    correlate the assistant turn with its tool result.
    """
    by_index: dict[int, dict] = {}
    for fragment in fragments:
        idx = fragment.get("index", 0)
        slot = by_index.setdefault(
            idx, {"id": None, "function": {"name": "", "arguments": ""}}
        )
        if fragment.get("id"):
            slot["id"] = fragment["id"]
        fn = fragment.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]
    for idx, slot in by_index.items():
        if not slot["id"]:
            slot["id"] = f"call_{step}_{idx}"
        # A tool with no parameters streams no argument fragments at all, so
        # this stays "" — which Vertex rejects with "Expected function
        # 'arguments' ... to be populated". The failure surfaces on the NEXT
        # request, after the tool has already run, so it reads as a
        # mid-conversation break rather than a malformed message.
        if not slot["function"]["arguments"]:
            slot["function"]["arguments"] = "{}"
    return [by_index[i] for i in sorted(by_index)]


def _summarise(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


async def run_stream(message: str, token: str | None = None) -> AsyncIterator[dict]:
    retries = [0]

    async with open_tools(token) as session:
        yield {
            "type": "status",
            "message": f"{len(session.schemas)} tools across {len(session.clients)} servers",
        }

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ]

        async with httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT_S) as http:
            for step in range(settings.MAX_STEPS):
                payload = {
                    "model": settings.LITELLM_MODEL,
                    "messages": messages,
                    "tools": session.schemas,
                    "tool_choice": "auto",
                    "temperature": settings.TEMPERATURE,
                }

                content = ""
                fragments: list[dict] = []
                try:
                    async for event in _stream_completion(http, payload, retries):
                        if "content" in event:
                            content += event["content"]
                            yield {"type": "token", "text": event["content"]}
                        else:
                            fragments.append(event["tool_delta"])
                except RateLimited as exc:
                    yield {"type": "error", "detail": str(exc)}
                    return
                except Exception as exc:
                    yield {"type": "error", "detail": str(exc)}
                    return

                calls = _assemble(fragments, step)

                if not calls:
                    yield {
                        "type": "done",
                        "steps": step,
                        "rateLimitRetries": retries[0],
                    }
                    return

                messages.append(
                    {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": c["function"],
                            }
                            for c in calls
                        ],
                    }
                )

                for call in calls:
                    name = call["function"]["name"]
                    raw = call["function"]["arguments"] or "{}"
                    try:
                        arguments = json.loads(raw)
                    except json.JSONDecodeError:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": "Error: arguments were not valid JSON.",
                            }
                        )
                        yield {
                            "type": "tool_result",
                            "name": name,
                            "ok": False,
                            "deniedBy": None,
                            "summary": "malformed arguments",
                        }
                        continue

                    yield {
                        "type": "tool_call",
                        "name": name,
                        "server": session.catalog.get(name, "?"),
                        "arguments": arguments,
                    }

                    result = await session.dispatch(name, arguments)
                    record = session.records[-1]
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": result[:6000],
                        }
                    )
                    yield {
                        "type": "tool_result",
                        "name": name,
                        "ok": record.ok,
                        "deniedBy": record.denied_by,
                        "summary": _summarise(record.error or record.result),
                    }

        yield {
            "type": "error",
            "detail": "Ran out of steps before reaching a conclusion.",
        }
