"""Asking the agent one question, the same way the chat pane does.

This goes through `waterplant_ui.harness` rather than talking to the agent
directly, which keeps one property worth protecting: a probe cannot reach
anything the participant's own console cannot. Whatever an attack achieves, the
agent agreed to do it when asked over the ordinary chat endpoint.

Each probe gets a fresh conversation id by default, so a prior probe's
transcript never primes the next one.

An attack run from the console is the exception: it passes its own session_id
and an on_turn callback, so the turns appear in the participant's transcript
and the console adopts that session for whatever is typed next. The point is
that "ask the agent why it just did that" has to work — a transcript showing a
conversation the agent cannot remember is the same class of lie as reporting a
blocked attack that never ran.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from .. import guardrails, harness

#: Called with ("user" | "assistant" | "system", text) as a conversation runs.
TurnEcho = Callable[[str, str], Awaitable[None]]


@dataclass
class TurnResult:
    text: str = ""
    error: str = ""
    #: The conversation id this probe ran under. hermes_otel records it on the
    #: trace as `mlflow.trace.session`, which is what lets a result be matched
    #: to its trace exactly rather than by "the most recent one".
    session_id: str = ""
    #: Stopped by the input guardrail rather than by a fault. Both leave `error`
    #: set and `ok` false — which is the verdict an attack wants either way —
    #: but only one of them means a control worked, and a reader of the detail
    #: should never have to guess which.
    blocked: bool = False
    #: The rails that refused, when `blocked`.
    rails: tuple[str, ...] = ()
    #: The operator stopped the case between turns, so the prompts after this
    #: one were never sent. Not an error: what came back is a real reply to a
    #: real turn, and the judge still has to score it.
    stopped: bool = False

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
    session_id: str = "",
    on_turn: TurnEcho | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> TurnResult:
    """Send one or more turns and return the last reply.

    Multi-turn cases (the account-takeover probe) need the turns to land in one
    conversation: the second turn only means anything after the first has
    established the frame. A transport failure on any turn aborts the rest —
    judging a half-delivered attack would be judging nothing.

    `session_id` joins an existing conversation instead of minting one.
    `on_turn(role, text)` is called as each turn goes out and each reply comes
    back, which is what puts an attack on screen live rather than as a block
    at the end. Both default to the old behaviour: own session, no echo.

    `should_stop()` is checked between turns, and returning True ends the
    conversation with whatever has come back so far. Between turns is the only
    honest place: the turn in flight is already being served by the agent, and
    there is nothing here that could un-send it.
    """
    if not agent_url:
        if on_turn:
            await on_turn("system", "No agent is configured, so nothing was sent.")
        return TurnResult(error="no agent configured")

    session = session_id or str(uuid.uuid4())
    history: list[dict[str, str]] = []
    reply = ""

    for prompt in prompts:
        # `reply` guards the first turn: a case that sent nothing at all
        # cannot be judged, and calling that "stopped part-way" would hide
        # that nothing was tested.
        if should_stop is not None and reply and should_stop():
            if on_turn:
                await on_turn("system", "Stopped by the operator.")
            return TurnResult(text=reply, session_id=session, stopped=True)

        if on_turn:
            await on_turn("user", prompt)

        # The same check the chat pane applies, for the same reason this module
        # goes through `harness` at all: a probe must not reach anything the
        # participant's own console cannot. An attack that bypassed the
        # guardrail would report "got through" against a control that was never
        # asked.
        verdict = await guardrails.check_input(prompt)
        if not verdict.allowed:
            detail = f"blocked by NeMo Guardrails — {verdict.summary}"
            if on_turn:
                await on_turn("system", detail)
            return TurnResult(
                error=detail, blocked=True, rails=verdict.rails, session_id=session
            )

        target, payload = harness.request(
            agent_url, prompt, persona, history, stream=False, session_id=session
        )
        headers = harness.headers_for(None, session_id=session)
        try:
            response = await client.post(
                target, json=payload, headers=headers, timeout=timeout_s
            )
        except httpx.HTTPError as exc:
            if on_turn:
                await on_turn("system", f"agent unreachable: {exc}")
            return TurnResult(error=f"agent unreachable: {exc}", session_id=session)

        try:
            body = harness.translate_reply(response.json())
        except ValueError:
            error = f"agent returned HTTP {response.status_code}: {response.text[:200]}"
            if on_turn:
                await on_turn("system", error)
            return TurnResult(error=error, session_id=session)

        if response.status_code >= 400 or "reply" not in body:
            detail = body.get("detail") or body.get("error") or response.text[:200]
            if on_turn:
                await on_turn("system", f"agent error: {detail}")
            return TurnResult(error=f"agent error: {detail}", session_id=session)

        reply = body.get("reply") or ""
        if on_turn:
            await on_turn("assistant", reply)
        # Hermes reloads its own transcript from the session id, so this is
        # only here for the native protocol, which keeps no session.
        history += [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": reply}]

    return TurnResult(text=reply, session_id=session)
