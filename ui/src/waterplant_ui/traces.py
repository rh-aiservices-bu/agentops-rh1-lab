"""Finding the MLflow trace a turn just produced.

Hermes loops server-side, so no tool call reaches this console (see
`harness.py`) — but the calls are not lost: the `hermes_otel` plugin exports
them to MLflow as TOOL spans. This module closes the gap by looking up *which*
trace the turn just wrote, so the console can link to that trace rather than to
MLflow's front door and leave the participant hunting.

Everything here was verified against the deployed MLflow (3.14.0) rather than
taken from the docs, because three of its behaviours are not guessable:

- **Search is `POST /api/3.0/mlflow/traces/search`, and `locations` must be
  structured.** `{"locations": ["2"]}` returns `200 {}` — an empty result, not
  an error — so a wrong shape here looks exactly like "no traces yet".
- **`order_by` is validated against a fixed key set.** `timestamp` and
  `timestamp_ms` sort; `request_time` — the field name the response itself
  uses — is rejected. The default order is already newest-first, so the sort is
  belt-and-braces.
- **A trace carries `request_preview`: the user's message, verbatim.** That is
  what makes correlation exact instead of a guess. Time alone would be a
  heuristic, and a wrong trace is worse than no link in an exercise whose whole
  point is reading the trace.

Authorization is Kubernetes RBAC, not an MLflow concept: the request is
authenticated with a ServiceAccount token and scoped by `X-MLflow-Workspace`.
The console's ServiceAccount needs `get`/`list` on `experiments` in group
`mlflow.kubeflow.org` in that namespace — without it every lookup 403s, and the
authorizer caches the denial for 300s, so a missing RoleBinding does not heal
the moment it is granted.

Nothing here may break a turn. Every failure — no config, no token, 403, a slow
export, a malformed body — degrades to "no link", never to a broken stream.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

#: What the *browser* opens: the public route, as a base with no trailing
#: path. The chart's default is
#: `https://rh-ai.<domain>/develop-train/mlflow` — MLflow as embedded in the
#: RHOAI dashboard, which is path-routed. Every link in this file is built by
#: appending to it.
#:
#: The same host also serves a standalone MLflow at the bare `/mlflow`, and
#: that one is hash-routed (`#/experiments/<id>/traces`). The two are not
#: interchangeable: point this at the bare `/mlflow` and `trace_url` builds a
#: path the standalone SPA has no route for. Both were confirmed serving on
#: cluster-zwtpw.
MLFLOW_URL = os.environ.get("MLFLOW_URL", "").rstrip("/")
#: What *this pod* calls, when that differs from the browser-facing route.
#:
#: Both serve the REST API under a `/mlflow` prefix, and the two URLs carry it
#: differently: MLFLOW_URL already ends in `/mlflow` — whichever of the two
#: routes it names, both end in that segment — while the in-cluster Service
#: (`https://mlflow.redhat-ods-applications.svc.cluster.local:8443`) does not.
#: This file used to document the opposite — "the in-cluster service serves the
#: REST API at its root with no /mlflow prefix" — and built paths to match, so
#: against the Service every lookup 404'd. Verified on cluster-pz7xk:
#: `/api/2.0/mlflow/experiments/get-by-name` answers 404 and
#: `/mlflow/api/2.0/mlflow/experiments/get-by-name` answers 200.
#:
#: `_api` normalises it, so neither callers nor a future reader has to
#: remember which form they were handed.
API_URL = os.environ.get("MLFLOW_API_URL", "").rstrip("/") or os.environ.get(
    "MLFLOW_URL", ""
).rstrip("/")


def _api(path: str) -> str:
    """An absolute API URL, whichever base this deployment was given."""
    base = API_URL if API_URL.endswith("/mlflow") else f"{API_URL}/mlflow"
    return f"{base}{path}"
#: The experiment Hermes writes to, named after the participant — the same
#: value the bridge passes the plugin as `MLFLOW_EXPERIMENT`. The id can be set
#: directly to skip the name lookup; otherwise it is resolved once and cached.
EXPERIMENT_NAME = os.environ.get("MLFLOW_EXPERIMENT", "").strip()
EXPERIMENT_ID = os.environ.get("MLFLOW_EXPERIMENT_ID", "").strip()
#: MLflow scopes data by workspace, and the workspace is the namespace whose
#: RBAC is consulted. Defaults to our own namespace, which is where the
#: participant's experiment lives.
WORKSPACE = (
    os.environ.get("MLFLOW_WORKSPACE", "").strip() or os.environ.get("NAMESPACE", "").strip()
)
#: Projected ServiceAccount token. Read per lookup, never cached: the projection
#: is rotated and a cached copy would start 401ing hours into a workshop.
TOKEN_FILE = os.environ.get(
    "MLFLOW_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"
)
#: Set when MLFLOW_URL points at the in-cluster service, whose certificate is
#: issued by openshift-service-serving-signer rather than a public root.
#: MLflow is served with an openshift-service-serving-signer certificate that
#: no public trust store knows, so talking to the in-cluster Service without
#: this fails verification — silently, because find_trace swallows transport
#: errors and a console that cannot reach MLflow is still a working console.
#: Defaulted to where tenant-platform mounts the mlflow-service-ca ConfigMap,
#: so the common case needs no configuration.
CA_FILE = os.environ.get("MLFLOW_CA_FILE", "").strip() or (
    "/etc/mlflow-ca/service-ca.crt"
    if os.path.isfile("/etc/mlflow-ca/service-ca.crt")
    else ""
)
#: Spans are exported after the turn ends, so the trace does not exist the
#: instant the answer does. Poll briefly rather than fetch once.
LOOKUP_SECONDS = float(os.environ.get("MLFLOW_TRACE_LOOKUP_SECONDS", "8"))
LOOKUP_INTERVAL_S = float(os.environ.get("MLFLOW_TRACE_LOOKUP_INTERVAL_S", "0.75"))
#: How far back a turn's trace may be timestamped. Generous: the export carries
#: the turn's own start time, not the moment it was written.
WINDOW_MS = int(float(os.environ.get("MLFLOW_TRACE_WINDOW_S", "900")) * 1000)

_client: httpx.AsyncClient | None = None
_experiment_id: str | None = EXPERIMENT_ID or None


def enabled() -> bool:
    """Is a per-turn trace link possible at all on this deployment?"""
    return bool(MLFLOW_URL and WORKSPACE and (EXPERIMENT_ID or EXPERIMENT_NAME))


def landing_url() -> str:
    """Where to send someone who wants MLflow but not one specific trace.

    The experiment *list*, scoped to this participant's workspace, rather than
    MLFLOW_URL bare. A bare base lands on RHOAI's experiment list for whatever
    workspace the session last used — which for a participant is usually not
    theirs, so they see an empty table and conclude tracing is broken. One
    query parameter is the difference between that and their own experiment
    sitting there named after them.

    Falls back to the bare base when there is no workspace to scope to, which
    is better than a link to nowhere.
    """
    if not MLFLOW_URL:
        return ""
    if not WORKSPACE:
        return f"{MLFLOW_URL}/experiments"
    return f"{MLFLOW_URL}/experiments?workspace={WORKSPACE}"


async def experiment_id() -> str:
    """This participant's experiment id, or "" when it cannot be resolved.

    The one way either link gets an id. Both the per-turn "see the trace"
    link and the footer's traces link need it, under the same preconditions
    and with the same answer: a second path would be a second way for them
    to disagree about which experiment is the participant's. Cached for the
    process by `_resolve_experiment_id` — the experiment is created once per
    participant and does not change under a running console.
    """
    if not enabled():
        return ""
    token = _token()
    if not token:
        return ""
    try:
        return await _resolve_experiment_id(token)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        # A console that cannot reach MLflow is still a working console.
        return ""


async def traces_tab_url() -> str:
    """Where the footer link goes: this participant's own Traces tab.

    `landing_url` is the experiment *list*, which is one more click and one
    more decision than a link labelled "MLflow traces" promises — and a
    participant who has never opened MLflow does not know which row is
    theirs. The list stays as the fallback, because the tab needs an id and
    an id can only be resolved over the API: when that fails the link still
    lands somewhere true rather than on a 404 built from a guess.
    """
    if not MLFLOW_URL:
        return ""
    found = await experiment_id()
    if not found:
        return landing_url()
    query = f"?workspace={WORKSPACE}" if WORKSPACE else ""
    return f"{MLFLOW_URL}/experiments/{found}/traces{query}"


def trace_url(trace_id: str, experiment_id: str) -> str:
    """A link that opens this exact trace.

    `experiments/:id/traces/:traceId` looks like the obvious route and is not
    one: MLflow's trace view is the experiment's traces tab with the trace
    selected through a query parameter, so the path form lands on an empty
    page. The workspace parameter is required too — without it the tab loads
    against whatever workspace the session last used, which for a participant
    is usually not theirs.

    Path-routed, because MLFLOW_URL names MLflow as embedded in the RHOAI
    dashboard. This was `/#/experiments/...` when the link pointed at the
    standalone MLflow shell on the same host; the embedded app routes on the
    path instead, and a fragment would never reach its router.
    """
    query = f"selectedEvaluationId={trace_id}"
    if WORKSPACE:
        query = f"workspace={WORKSPACE}&{query}"
    return f"{MLFLOW_URL}/experiments/{experiment_id}/traces?{query}"


def _client_for_mlflow() -> httpx.AsyncClient:
    # Deliberately not the client `app.py` uses for the agent: that one may
    # carry the OpenShell gateway's client certificate and trust only its CA,
    # which would fail against MLflow's certificate and send a credential
    # somewhere it has no business going.
    global _client
    if _client is None:
        verify: Any = CA_FILE if CA_FILE else True
        _client = httpx.AsyncClient(verify=verify, timeout=10.0)
    return _client


def _token() -> str:
    try:
        return Path(TOKEN_FILE).read_text().strip()
    except OSError:
        return ""


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-MLflow-Workspace": WORKSPACE,
        "Content-Type": "application/json",
    }


async def _resolve_experiment_id(token: str) -> str:
    global _experiment_id
    if _experiment_id:
        return _experiment_id
    resp = await _client_for_mlflow().get(
        _api("/api/2.0/mlflow/experiments/get-by-name"),
        params={"experiment_name": EXPERIMENT_NAME},
        headers=_headers(token),
    )
    if resp.status_code != 200:
        return ""
    found = (resp.json().get("experiment") or {}).get("experiment_id", "")
    # Cached for the process: the experiment is created once per participant at
    # sandbox provision time and does not change under a running console.
    _experiment_id = found or None
    return found


def _matches(trace: dict, message: str) -> bool:
    """Is this the trace for the message we just sent?

    `request_preview` is JSON-encoded and, being a preview, may be truncated —
    so compare on a normalised prefix rather than for equality.
    """
    preview = trace.get("request_preview") or ""
    try:
        decoded = json.loads(preview)
    except ValueError:
        decoded = preview
    if not isinstance(decoded, str):
        return False
    a = " ".join(decoded.split()).casefold()
    b = " ".join(message.split()).casefold()
    if not a or not b:
        return False
    return b.startswith(a[:120]) or a.startswith(b[:120])


async def _search(token: str, experiment_id: str, since_ms: int) -> list[dict]:
    body = {
        "locations": [
            {"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": experiment_id}}
        ],
        "max_results": 10,
        "order_by": ["timestamp_ms DESC"],
        "filter": f"attributes.timestamp > {since_ms}",
    }
    resp = await _client_for_mlflow().post(
        _api("/api/3.0/mlflow/traces/search"), json=body, headers=_headers(token)
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("traces") or []


async def find_trace(message: str, started_ms: int) -> tuple[str, str] | None:
    """(trace_id, experiment_id) for this turn, or None if it cannot be pinned.

    Returns only on an exact match with the message that was sent. Falling back
    to "the newest trace" would be wrong precisely when it matters — two turns
    in flight, or an export still pending — and would point a participant at
    another turn's tool calls while they debug their own.
    """
    if not enabled():
        return None
    token = _token()
    if not token:
        return None
    # The same resolver the footer link uses, so the two can never point at
    # different experiments.
    found = await experiment_id()
    if not found:
        return None
    try:
        deadline = time.monotonic() + LOOKUP_SECONDS
        since_ms = max(0, started_ms - WINDOW_MS)
        while True:
            for trace in await _search(token, found, since_ms):
                if _matches(trace, message) and trace.get("trace_id"):
                    return trace["trace_id"], found
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(LOOKUP_INTERVAL_S)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        # A console that cannot reach MLflow is still a working console.
        return None
