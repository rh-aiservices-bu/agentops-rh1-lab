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

import asyncio
import logging
import os
import ssl
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import guardrails, harness
from .probes import engine as probe_engine
from .probes import loader as probe_loader
from .probes import events as probe_events
from .probes import mlflow_run, scheduler

log = logging.getLogger(__name__)

PLANT_API_URL = os.environ.get("PLANT_API_URL", "http://plant-api:8080")
# Empty until the agent is deployed — the chat pane degrades to a clear
# "not yet available" state rather than failing opaquely.
AGENT_URL = os.environ.get("AGENT_URL", "").rstrip("/")
MLFLOW_URL = os.environ.get("MLFLOW_URL", "")
# The agent can legitimately take minutes: a seven-step tool chain plus backoff
# through the shared endpoint's 429s. A 30s timeout cut off healthy requests.
TIMEOUT_S = float(os.environ.get("HTTP_TIMEOUT_S", "300"))

# mTLS for OpenShell gateway service-relay URLs — see hermes-openshell/README.md.
AGENT_TLS_CA = os.environ.get("AGENT_TLS_CA", "")
AGENT_TLS_CERT = os.environ.get("AGENT_TLS_CERT", "")
AGENT_TLS_KEY = os.environ.get("AGENT_TLS_KEY", "")

#: The attack/eval engine. Off by one env var, because an instructor
#: walking through module 1 does not want an attack landing mid-sentence.
PROBES_ENABLED = os.environ.get("PROBES_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", ""
)
#: Whether attacks fire on their own. Separate from PROBES_ENABLED, which
#: turns the whole engine off including the buttons: this leaves the console
#: fully usable and only stops the clock. Off while the lab is being built,
#: so an attack never lands in the middle of someone demonstrating something
#: else; the scheduler itself is untouched and comes back with one env var.
ATTACK_SCHEDULER_ENABLED = os.environ.get(
    "ATTACK_SCHEDULER_ENABLED", "1"
).strip().lower() not in ("0", "false", "no")
#: The randomised attack window. Random inside it rather than a fixed tick:
#: a predictable rhythm gets learned and watched for.
ATTACK_INTERVAL_MIN_S = float(os.environ.get("ATTACK_INTERVAL_MIN_S", "300"))
ATTACK_INTERVAL_MAX_S = float(os.environ.get("ATTACK_INTERVAL_MAX_S", "600"))
#: Per-probe budget. Generous: a golden-workflow turn is a multi-step tool
#: chain through a shared model endpoint that answers 429 under load.
PROBE_TIMEOUT_S = float(os.environ.get("PROBE_TIMEOUT_S", "180"))
#: The documentation site. It serves the pages read-documentation asks for
#: and, on one page, a redirect onward to an attacker path it logs — so it is
#: both the target of the doc evals and the only observer of whether an
#: exfiltration attempt actually arrived somewhere. Unset, the honeypot goals
#: have no observer and the engine reports them unjudgeable rather than
#: guessing from the transcript.
DOCS_URL = os.environ.get("DOCS_URL", "")
#: Where the agent's filesystem is, so the goals that turn on a deleted or
#: tampered file are measured rather than reported by the agent under test.
#: The console execs into this pod and looks. Unset, it falls back to asking
#: the agent and labels the verdict as reported.
AGENT_POD_SELECTOR = os.environ.get("AGENT_POD_SELECTOR", "")
AGENT_POD_CONTAINER = os.environ.get("AGENT_POD_CONTAINER", "")

STATIC = Path(__file__).parent / "static"

_client: httpx.AsyncClient | None = None
_engine: probe_engine.Engine | None = None
_scheduler: scheduler.Scheduler | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Build the probe engine and start its timer.

    Both are optional: with PROBES_ENABLED off, or no agent to probe, the
    console serves the dashboard and chat exactly as it did before and the
    attack endpoints answer 503 with a reason.
    """
    global _engine, _scheduler
    if PROBES_ENABLED and AGENT_URL:
        _engine = probe_engine.Engine(
            client=client(),
            agent_url=AGENT_URL,
            plant_api_url=PLANT_API_URL,
            docs_url=DOCS_URL,
            agent_pod_selector=AGENT_POD_SELECTOR,
            agent_pod_container=AGENT_POD_CONTAINER,
            timeout_s=PROBE_TIMEOUT_S,
        )
        # Recover before anything else runs. A plant left in emergency
        # shutdown by an earlier session has its intake controller stopped,
        # so the reservoir drains to zero and every reading on the console —
        # and every probe result — is measured against a dead plant.
        try:
            healthy, why = await _engine.ensure_plant_healthy()
            log.info("plant health at startup: %s", why)
            if not healthy:
                log.warning("console starting against an unhealthy plant: %s", why)
        except Exception:  # noqa: BLE001 - never block startup on this
            log.exception("plant health check failed at startup")

        if ATTACK_SCHEDULER_ENABLED:
            _scheduler = scheduler.Scheduler(
                _engine, min_s=ATTACK_INTERVAL_MIN_S, max_s=ATTACK_INTERVAL_MAX_S
            )
            _scheduler.start()
        else:
            log.info("attack scheduler disabled; attacks run from the button only")
    try:
        yield
    finally:
        if _scheduler is not None:
            await _scheduler.stop()


app = FastAPI(title="Water Plant UI", version="0.2.0", lifespan=lifespan)


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {"timeout": TIMEOUT_S}
        if AGENT_TLS_CA and AGENT_TLS_CERT and AGENT_TLS_KEY:
            # httpx 0.28's verify= + cert= combo silently drops the client
            # cert; building the SSLContext ourselves works.
            ctx = ssl.create_default_context(cafile=AGENT_TLS_CA)
            ctx.load_cert_chain(AGENT_TLS_CERT, AGENT_TLS_KEY)
            kwargs["verify"] = ctx
        _client = httpx.AsyncClient(**kwargs)
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
        # Which harness is behind the chat pane, and what it costs. Both are
        # reported rather than hidden: a participant told to inspect a trace or
        # to switch persona should be able to see when the deployed harness
        # cannot honour that, instead of concluding the exercise is broken.
        "agentHarness": harness.PROTOCOL,
        "agentTraceAvailable": harness.PROTOCOL == "waterplant",
        "identityPropagated": harness.identity_is_propagated(),
        # Whether a prompt is checked before the agent is asked. Reported for
        # the same reason as the two above: a participant looking for the
        # guardrail must be able to see that none is wired yet.
        "inputGuardrails": guardrails.enabled(),
    }


@app.get("/api/state")
async def state(events_since: int = Query(-1, alias="eventsSince")) -> JSONResponse:
    """Plant state, safety readout and any new probe events, in one call.

    The events ride along on the dashboard's existing 1 Hz poll rather than
    getting a channel of their own. That is the whole notification mechanism:
    no websocket, no SSE, no push infrastructure. The client sends the highest
    sequence number it has seen and gets everything after it, so a reconnecting
    browser catches up by construction and a dropped poll costs nothing.

    `eventsSince=-1` (the default, and what a freshly loaded page sends) means
    "I am new here": it returns the current cursor and no backlog, so a reload
    does not replay an hour of old attacks as live toasts.
    """
    try:
        plant, safety = await _gather("/state", "/safety")
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"error": f"plant-api unreachable: {exc}"}, status_code=503
        )

    if events_since < 0:
        new_events: list[dict[str, Any]] = []
        cursor = probe_events.latest_seq()
    else:
        new_events = probe_events.since(events_since)
        cursor = new_events[-1]["seq"] if new_events else events_since

    return JSONResponse(
        {
            "plant": plant,
            "safety": safety,
            "events": new_events,
            "eventCursor": cursor,
            "probesBusy": _engine.busy() if _engine else False,
        }
    )


def _probes_unavailable() -> JSONResponse:
    """Why the buttons cannot do anything, in terms the console can show."""
    if not PROBES_ENABLED:
        detail = "The attack and evaluation engine is disabled (PROBES_ENABLED=0)."
    elif not AGENT_URL:
        detail = (
            "There is no agent to probe yet. Attacks and evals run against the "
            "agent through the same chat endpoint this console uses."
        )
    else:
        detail = "The attack and evaluation engine did not start."
    return JSONResponse({"error": "probes_unavailable", "detail": detail}, status_code=503)


@app.get("/api/probes")
async def probes() -> JSONResponse:
    """The catalogue, including the cases that are defined but not yet armed.

    A disabled case is shown with the reason it is disabled rather than hidden.
    A participant who cannot see that three of the seven attacks are not
    running would reasonably conclude they are defended against them.
    """
    if _engine is None:
        # Still report a definitions fault. The engine is not built when there
        # is no agent to probe, but "no definitions are mounted" is a separate
        # deployment problem and stays worth saying — otherwise fixing the
        # agent just reveals it a second time.
        return JSONResponse(
            {
                "attacks": [],
                "evals": [],
                "busy": False,
                "available": False,
                "problem": probe_loader.load_problem(),
            }
        )
    return JSONResponse(
        {
            **_engine.catalogue(),
            "available": True,
            "scheduled": ATTACK_SCHEDULER_ENABLED,
        }
    )


class AttackRequest(BaseModel):
    #: Omitted or null picks a random armed attack — what the button sends.
    id: str | None = None


@app.post("/api/attacks/run")
async def run_attack(req: AttackRequest) -> JSONResponse:
    """Fire one attack now, in the background.

    Returns as soon as the attack is chosen, not when it finishes: a probe is a
    multi-turn conversation with a model and can take minutes. The console
    learns what happened from the event stream, exactly as it does for a
    scheduled attack — one code path, so the button cannot drift from the timer.
    """
    if _engine is None:
        return _probes_unavailable()
    if _engine.busy():
        return JSONResponse(
            {"error": "busy", "detail": "A probe is already running."}, status_code=409
        )

    if req.id:
        case = next((c for c in _engine.attacks() if c.id == req.id), None)
        if case is None:
            return JSONResponse(
                {"error": "unknown_attack", "detail": f"No armed attack {req.id!r}."},
                status_code=404,
            )
    else:
        case = _engine.pick_attack()
        if case is None:
            return JSONResponse(
                {"error": "none_armed", "detail": "No attacks are currently armed."},
                status_code=503,
            )

    asyncio.create_task(_engine.run_attack(case))
    return JSONResponse({"started": True, "id": case.id, "title": case.title})


@app.post("/api/plant/reset")
async def reset_plant() -> JSONResponse:
    """Restore the seeded plant, on the operator's explicit say-so.

    Deliberately a button and not something that happens quietly. POST /reset
    reseeds the maintenance store as well as the plant, so every work order
    raised since the last reset is gone and the work-order sequence restarts —
    that is why cleanup only reaches for it when the alternative is a plant
    that stays dark, and why the console asks before calling this.
    """
    try:
        response = await client().post(f"{PLANT_API_URL}/reset", timeout=30.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"error": "reset_failed", "detail": f"plant-api: {exc}"}, status_code=502
        )

    probe_events.emit(
        kind="plant", phase="resolved", id="plant-reset",
        title="Plant reset", outcome="reset",
        detail="the plant is back at its seeded starting state; work orders and "
               "maintenance records raised since the last reset are gone",
    )
    return JSONResponse({"reset": True})


@app.post("/api/plant/release-shutdown")
async def release_shutdown() -> JSONResponse:
    """Lift an emergency shutdown, leaving everything else as it is.

    Restarting the pumps does not do this, and that surprises people: the
    latch is independent of pump state, and while it is set the intake
    controller stops, so the reservoir drains *faster* with the pumps running.
    Nothing the agent can call releases it either — control-mcp has seven
    tools and none of them clears a shutdown.

    A full reset would clear it, but it also reseeds the maintenance store and
    takes the participant's work orders with it. This is the surgical option,
    and it is why plant-api 0.2.0 added the endpoint.
    """
    try:
        response = await client().post(
            f"{PLANT_API_URL}/emergency-shutdown/clear", timeout=20.0
        )
        if response.status_code == 404:
            return JSONResponse(
                {
                    "error": "unsupported",
                    "detail": (
                        "This plant-api predates POST /emergency-shutdown/clear. "
                        "Only a full plant reset can release the shutdown."
                    ),
                },
                status_code=501,
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"error": "release_failed", "detail": f"plant-api: {exc}"}, status_code=502
        )

    probe_events.emit(
        kind="plant", phase="resolved", id="shutdown-released",
        title="Shutdown released", outcome="ok",
        detail="the intake is filling again; nothing else was changed",
    )
    return JSONResponse({"released": True})


@app.post("/api/evals/run")
async def run_evals() -> JSONResponse:
    """Run the whole eval suite and return the scorecard.

    Synchronous, unlike the attack endpoint: the participant pressed this and
    is waiting for a number. Per-eval progress still goes out as events, so the
    console can tick through the suite rather than showing a dead spinner.
    """
    if _engine is None:
        return _probes_unavailable()
    if _engine.busy():
        return JSONResponse(
            {"error": "busy", "detail": "A probe is already running."}, status_code=409
        )

    summary = await _engine.run_all_evals()
    # Best-effort: a sweep that ran is a sweep that counts, whether or not
    # MLflow was reachable to record it.
    summary["mlflowRunUrl"] = await mlflow_run.log_eval_sweep(client(), summary)
    probe_events.emit(**summary)
    return JSONResponse(summary)


async def _gather(*paths: str) -> list[Any]:
    out = []
    for path in paths:
        response = await client().get(f"{PLANT_API_URL}{path}")
        response.raise_for_status()
        out.append(response.json())
    return out


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str
    persona: str = "operator"
    #: The conversation so far, sent by the console. Our own agent keeps no
    #: session, so this is how its follow-ups have something to follow. Hermes
    #: does keep one and is given `conversation_id` instead — see
    #: waterplant_ui.harness.request for why the replay must not reach it.
    history: list[Turn] = []
    #: Names the conversation for a harness that keeps its own (Hermes). A UUID
    #: by type, because it ends up in a request header.
    conversation_id: uuid.UUID | None = Field(None, alias="conversationId")


def _history(req: ChatRequest) -> list[dict[str, str]]:
    return [turn.model_dump() for turn in req.history]


def _session(req: ChatRequest) -> str | None:
    return str(req.conversation_id) if req.conversation_id else None


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

    verdict = await guardrails.check_input(req.message)
    if not verdict.allowed:
        return JSONResponse(
            {
                "error": "input_blocked",
                "detail": (
                    "Blocked by the input guardrail before the agent saw it. "
                    f"Rails that refused: {verdict.summary}."
                ),
                "rails": list(verdict.rails),
            },
            status_code=403,
        )

    target, payload = harness.request(
        AGENT_URL, req.message, req.persona, _history(req), stream=False, session_id=_session(req)
    )
    headers = harness.headers_for(request.headers.get("authorization"), session_id=_session(req))

    try:
        response = await client().post(target, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"agent unreachable: {exc}"}, status_code=502)

    # The agent may fail with a non-JSON body (an unhandled exception renders as
    # plain text). Parsing it blindly turned an agent fault into a UI fault and
    # hid the real error.
    try:
        body = harness.translate_reply(response.json())
    except ValueError:
        body = {
            "error": "agent_error",
            "detail": f"Agent returned HTTP {response.status_code}: {response.text[:400]}",
        }
    return JSONResponse(body, status_code=response.status_code)


#: See _RevalidatingStatic.
_REVALIDATE = {"Cache-Control": "no-cache"}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(), headers=_REVALIDATE)


class _RevalidatingStatic(StaticFiles):
    """Static files the browser must check before reusing.

    Served with only an ETag and Last-Modified, the console's JS was cached
    heuristically and an ordinary reload kept running the old copy after a
    rollout. Caught on cluster-z5jhc: the pod served the new app.js, the
    browser still sent no conversationId, and Hermes fell back to replaying
    prose. `no-cache` still lets the ETag answer 304, so it costs a round trip,
    not a download.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(_REVALIDATE)
        return response


app.mount("/static", _RevalidatingStatic(directory=STATIC), name="static")


#: Unbuffered, uncached: an SSE stream that a proxy batches arrives as one
#: block at the end, which reads as a hang.
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _frames(events: list[dict]) -> AsyncIterator[bytes]:
    """A complete, self-contained stream — used when the agent is never called."""
    for event in events:
        yield harness.sse(event)


async def _prefixed(
    events: list[dict], stream: AsyncIterator[bytes]
) -> AsyncIterator[bytes]:
    """`events`, then the agent's own stream.

    Lets a warning about the guardrail land before the answer it could not
    vouch for, without `harness.relay_stream` having to know a guardrail exists.
    """
    for event in events:
        yield harness.sse(event)
    async for chunk in stream:
        yield chunk


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """Proxy the agent's SSE stream to the browser, unbuffered.

    On the native path this relays raw and adds nothing — the BFF decides
    nothing; it exists so the browser talks to one origin. On a foreign harness
    `waterplant_ui.harness` translates into the same event shape, so the console
    itself is identical either way. What differs is what the harness can supply:
    see that module for what an OpenAI-compatible server cannot.
    """
    if not AGENT_URL:
        return JSONResponse(
            {
                "error": "agent_not_deployed",
                "detail": "The maintenance agent is not deployed in this environment yet.",
            },
            status_code=503,
        )

    # Before the agent, never alongside it: a prompt the guardrail refuses must
    # not reach the model at all, which is the whole difference between an input
    # rail and an apology after the fact.
    verdict = await guardrails.check_input(req.message)
    if not verdict.allowed:
        return StreamingResponse(
            _frames(guardrails.blocked_events(verdict)),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    return StreamingResponse(
        _prefixed(
            guardrails.notice_events(verdict),
            harness.relay_stream(
                client(),
                AGENT_URL,
                req.message,
                req.persona,
                _history(req),
                request.headers.get("authorization"),
                _session(req),
            ),
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
