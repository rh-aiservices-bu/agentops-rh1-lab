"""Asking the agent one question, the same way the chat pane does.

This goes through `waterplant_ui.harness` rather than talking to the agent
directly, which keeps one property worth protecting: a probe cannot reach
anything the participant's own console cannot. Whatever an attack achieves, the
agent agreed to do it when asked over the ordinary chat endpoint.

Each probe gets a fresh conversation id, so a prior probe's transcript never
primes the next one and the participant's own conversation is never joined.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx

from .. import harness


@dataclass
class TurnResult:
    text: str = ""
    error: str = ""
    #: The conversation id this probe ran under. hermes_otel records it on the
    #: trace as `mlflow.trace.session`, which is what lets a result be matched
    #: to its trace exactly rather than by "the most recent one".
    session_id: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


async def ask(
    client: httpx.AsyncClient,
    agent_url: str,
    prompts: list[str],
    *,
    persona: str = "operator",
    timeout_s: float = 180.0,
) -> TurnResult:
    """Send one or more turns and return the last reply.

    Multi-turn cases (the account-takeover probe) need the turns to land in one
    conversation: the second turn only means anything after the first has
    established the frame. A transport failure on any turn aborts the rest —
    judging a half-delivered attack would be judging nothing.
    """
    if not agent_url:
        return TurnResult(error="no agent configured")

    session = str(uuid.uuid4())
    history: list[dict[str, str]] = []
    reply = ""

    for prompt in prompts:
        target, payload = harness.request(
            agent_url, prompt, persona, history, stream=False, session_id=session
        )
        headers = harness.headers_for(None, session_id=session)
        try:
            response = await client.post(
                target, json=payload, headers=headers, timeout=timeout_s
            )
        except httpx.HTTPError as exc:
            return TurnResult(error=f"agent unreachable: {exc}", session_id=session)

        try:
            body = harness.translate_reply(response.json())
        except ValueError:
            return TurnResult(
                error=f"agent returned HTTP {response.status_code}: {response.text[:200]}",
                session_id=session,
            )

        if response.status_code >= 400 or "reply" not in body:
            detail = body.get("detail") or body.get("error") or response.text[:200]
            return TurnResult(error=f"agent error: {detail}", session_id=session)

        reply = body.get("reply") or ""
        # Hermes reloads its own transcript from the session id, so this is
        # only here for the native protocol, which keeps no session.
        history += [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": reply}]

    return TurnResult(text=reply, session_id=session)
