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

#: What the *browser* opens: the public route, which carries a `/mlflow` path
#: prefix.
MLFLOW_URL = os.environ.get("MLFLOW_URL", "").rstrip("/")
#: What *this pod* calls, when that differs. The in-cluster service
#: (`https://mlflow.redhat-ods-applications.svc.cluster.local:8443`) serves the
#: REST API at its root with **no `/mlflow` prefix** — the prefix belongs to the
#: route, not the API — so the two URLs are not interchangeable. Set this (with
#: MLFLOW_CA_FILE) if the pod cannot reach its own cluster's public route.
API_URL = os.environ.get("MLFLOW_API_URL", "").rstrip("/") or os.environ.get(
    "MLFLOW_URL", ""
).rstrip("/")
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
CA_FILE = os.environ.get("MLFLOW_CA_FILE", "").strip()
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


def root_url() -> str:
    return MLFLOW_URL


def trace_url(trace_id: str, experiment_id: str) -> str:
    # MLflow's UI is a hash router; `experiments/:experimentId/traces/:traceId`
    # is a real route, so this opens the trace itself rather than a filtered
    # list the participant still has to read.
    return f"{MLFLOW_URL}/#/experiments/{experiment_id}/traces/{trace_id}"


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
        f"{API_URL}/api/2.0/mlflow/experiments/get-by-name",
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
        f"{API_URL}/api/3.0/mlflow/traces/search", json=body, headers=_headers(token)
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
    try:
        experiment_id = await _resolve_experiment_id(token)
        if not experiment_id:
            return None
        deadline = time.monotonic() + LOOKUP_SECONDS
        since_ms = max(0, started_ms - WINDOW_MS)
        while True:
            for trace in await _search(token, experiment_id, since_ms):
                if _matches(trace, message) and trace.get("trace_id"):
                    return trace["trace_id"], experiment_id
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(LOOKUP_INTERVAL_S)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        # A console that cannot reach MLflow is still a working console.
        return None
