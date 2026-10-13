"""Writing one MLflow run per eval sweep.

Uses httpx rather than the `mlflow` package, matching traces.py next door: the
console image stays small and nothing here needs the client library's
model-registry half.

Three things about this deployment that are easy to get wrong, all found the
hard way against cluster-z5jhc:

**MLFLOW_URL is not an API endpoint.** It is the human-facing route through
the OpenShift OAuth proxy, so an unauthenticated POST from a pod gets a 302 to
the login page and a cheerful 200 full of HTML. The write path needs the
in-cluster Service instead — hence MLFLOW_API_URL, separate from the URL the
console hands the browser.

**The REST prefix is doubled.** The API lives at `<base>/mlflow/api/2.0/mlflow/...`
on the in-cluster Service. `/api/2.0/mlflow/...` returns 404. This is the same
split tenant-openshell/values.yaml warns about for OTLP spans, which go to the
server root while the REST API sits under /mlflow.

**Auth is Kubernetes, not MLflow.** The server runs --app-name=kubernetes-auth,
so every request becomes a SelfSubjectAccessReview against the (non-existent)
mlflow.kubeflow.org API group in the workspace named by X-MLflow-Workspace.
That means the pod's own ServiceAccount token, and a Role granting create and
update on `experiments` — get and list alone are enough to read a run and not
enough to write one.

Everything here is best-effort. MLflow being unreachable, unauthorised or not
configured must never turn a completed eval sweep into a failure: the results
are already on screen, and this only adds a durable record and a link to it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

#: Where the browser is sent. Behind the OAuth proxy, so never used for API
#: calls — only to build the link in the eval scorecard.
MLFLOW_URL = os.environ.get("MLFLOW_URL", "").rstrip("/")
#: Where this process talks to MLflow. The in-cluster Service.
MLFLOW_API_URL = os.environ.get("MLFLOW_API_URL", "").rstrip("/")
#: Where eval sweeps are recorded: the participant's own experiment, the same
#: one the agent's inference traces go to.
#:
#: These used to go to a separate `<username>-cycle` experiment, on the
#: reasoning that rollups would bury the participant's own turns. They do not:
#: MLflow lists traces and runs as separate things, so a sweep lands under
#: Runs and never appears among the Traces. One experiment means one place to
#: look, and the sweep sits next to the very turns it scored.
#:
#: MLFLOW_CYCLE_EXPERIMENT is still honoured when MLFLOW_EXPERIMENT is unset,
#: so a deployment rendered from the older chart keeps working.
EXPERIMENT = os.environ.get("MLFLOW_EXPERIMENT") or os.environ.get(
    "MLFLOW_CYCLE_EXPERIMENT", ""
)
NAMESPACE = os.environ.get("NAMESPACE", "")
#: How long to keep looking for the sweep's traces before giving up, and how
#: often to re-check.
#:
#: A single short sleep does not work. hermes_otel batches spans and flushes
#: on session end, and "session end" is not the moment the HTTP reply lands:
#: a sweep whose five turns were all traced still resolved none of them six
#: seconds later, and all five were present a minute after that. Polling costs
#: nothing when the traces are already there — the loop exits on the first
#: complete match — and recovers the common case where they are not.
TRACE_LOOKUP_SECONDS = float(os.environ.get("PROBE_TRACE_LOOKUP_S", "75"))
TRACE_LOOKUP_INTERVAL_S = float(os.environ.get("PROBE_TRACE_INTERVAL_S", "5"))

_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
#: Mounted from the mlflow-service-ca ConfigMap. MLflow is served with an
#: openshift-service-serving-signer certificate, which is in no public trust
#: store, so the default context fails with "self-signed certificate".
_CA_PATH = Path(os.environ.get("MLFLOW_CA_FILE", "/etc/mlflow-ca/service-ca.crt"))


def _token() -> str:
    try:
        return _TOKEN_PATH.read_text().strip()
    except OSError:
        return ""


def _ssl_context() -> ssl.SSLContext | bool:
    if _CA_PATH.is_file():
        return ssl.create_default_context(cafile=str(_CA_PATH))
    # No CA mounted: let httpx use its defaults and fail honestly rather than
    # silently disabling verification to talk to an unverified server.
    return True


def enabled() -> bool:
    return bool(MLFLOW_API_URL and EXPERIMENT)


def _api(path: str) -> str:
    return f"{MLFLOW_API_URL}/mlflow/api/2.0/mlflow{path}"


def _api3(path: str) -> str:
    """Traces are a 3.0-only API on this deployment; runs are still 2.0."""
    return f"{MLFLOW_API_URL}/mlflow/api/3.0/mlflow{path}"


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if NAMESPACE:
        headers["X-MLflow-Workspace"] = NAMESPACE
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _experiment_id(client: httpx.AsyncClient) -> str | None:
    try:
        response = await client.get(
            _api("/experiments/get-by-name"),
            params={"experiment_name": EXPERIMENT},
            headers=_headers(),
            timeout=15.0,
        )
        if response.status_code == 200:
            return response.json()["experiment"]["experiment_id"]
        # Not there yet — the first sweep of a fresh participant creates it.
        created = await client.post(
            _api("/experiments/create"),
            json={"name": EXPERIMENT},
            headers=_headers(),
            timeout=15.0,
        )
        created.raise_for_status()
        return created.json()["experiment_id"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("mlflow experiment lookup failed: %s", exc)
        return None


async def find_traces_by_session(
    client: httpx.AsyncClient, experiment_id: str, sessions: set[str]
) -> dict[str, str]:
    """session id -> trace id, for the sessions we are given.

    Matched on `mlflow.trace.session`, which hermes_otel writes from the
    X-Hermes-Session-Id the probe sent. That is an exact match to the probe
    that produced it — "the most recent trace" would be wrong precisely when
    it matters, with several turns in flight.

    Returns what it finds. A missing trace is normal: export is asynchronous,
    and a sweep can finish before the last batch lands.
    """
    if not sessions:
        return {}
    body = {
        "locations": [{
            "type": "MLFLOW_EXPERIMENT",
            "mlflow_experiment": {"experiment_id": experiment_id},
        }],
        # Enough to cover one sweep several times over without paging.
        "max_results": 200,
    }
    try:
        response = await client.post(
            _api3("/traces/search"), json=body, headers=_headers(), timeout=25.0
        )
        response.raise_for_status()
        traces = response.json().get("traces", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("mlflow trace lookup failed: %s", exc)
        return {}

    found = {}
    for trace in traces:
        meta = trace.get("trace_metadata") or {}
        session = meta.get("mlflow.trace.session")
        if session in sessions:
            found[session] = trace.get("trace_id", "")
    return found


async def _link_traces(
    client: httpx.AsyncClient, run_id: str, trace_ids: list[str]
) -> bool:
    """Attach the sweep's traces to its run, so the run's Evaluations tab
    shows them.

    `POST /api/2.0/mlflow/traces/link-to-run`, which sets `mlflow.sourceRun`
    on each trace server-side. Two things about it are easy to get wrong, and
    cost this a couple of rounds:

    * It lives under **2.0**, even though trace *search* on this server is
      3.0-only. Probing only `/api/3.0/...` returns ENDPOINT_NOT_FOUND, which
      reads as "this build cannot do it".
    * The result is not visible in a plain traces/search response — the
      metadata it sets is not echoed there. Confirm with a filtered search:
      ``filter: metadata.`mlflow.sourceRun` = '<run id>'``.

    Capped at 100 trace ids per request by the API; a sweep is far smaller,
    but the slice keeps that from becoming a surprise later.
    """
    if not trace_ids:
        return True
    try:
        response = await client.post(
            _api("/traces/link-to-run"),
            json={"run_id": run_id, "trace_ids": trace_ids[:100]},
            headers=_headers(),
            timeout=25.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # The sweep is recorded either way; this only costs the association.
        log.warning("linking traces to run %s failed: %s", run_id, exc)
        return False
    return True


async def _await_traces(
    client: httpx.AsyncClient, experiment_id: str, sessions: set[str]
) -> dict[str, str]:
    """Wait for the sweep's traces to land, returning whatever arrived.

    Gives up at the deadline rather than failing: a sweep that ran is a sweep
    that counts, and a missing link costs a link. Returns early as soon as
    every session has a trace, so a deployment where the exporter is prompt
    pays nothing for this.
    """
    if not sessions:
        return {}
    deadline = time.monotonic() + TRACE_LOOKUP_SECONDS
    found: dict[str, str] = {}
    while True:
        found = await find_traces_by_session(client, experiment_id, sessions)
        if len(found) == len(sessions) or time.monotonic() >= deadline:
            if len(found) < len(sessions):
                log.warning(
                    "eval sweep: only %d of %d traces resolved before the "
                    "deadline; those evals will have no trace link",
                    len(found), len(sessions),
                )
            return found
        await asyncio.sleep(TRACE_LOOKUP_INTERVAL_S)


async def trace_url_for_session(session: str) -> str:
    """The MLflow trace link for one conversation, or "" if there is none.

    Attacks had no trace link at all: the sweep resolves its traces inside
    _write, and nothing on the single-attack path ever did. So an attack's
    notification could not offer what every typed turn in the chat pane
    offers, which is the first thing a participant wants after seeing
    "got through".

    Waits the same way a sweep does, because the exporter is asynchronous and
    an attack usually resolves before its trace lands.
    """
    if not session or not enabled():
        return ""
    async with httpx.AsyncClient(verify=_ssl_context()) as client:
        experiment_id = await _experiment_id(client)
        if experiment_id is None:
            return ""
        found = await _await_traces(client, experiment_id, {session})
        trace_id = found.get(session, "")
        if not trace_id:
            return ""
        return (
            f"{MLFLOW_URL or MLFLOW_API_URL}/#/experiments/{experiment_id}"
            f"/traces?workspace={NAMESPACE}&selectedEvaluationId={trace_id}"
        )


async def log_eval_sweep(
    _unused: httpx.AsyncClient, summary: dict[str, Any]
) -> str | None:
    """Record one sweep. Returns a URL to the run, or None.

    Metrics are 1.0 for passed and 0.0 for failed. An inconclusive eval gets a
    *tag*, never a 0.0 metric: zero means failure, and a case the engine could
    not judge must not be recorded as one the participant failed.
    """
    if not enabled():
        return None

    # Its own client: the console's shared one is built for plant-api and the
    # agent, and has no reason to carry MLflow's service CA.
    async with httpx.AsyncClient(verify=_ssl_context()) as client:
        return await _write(client, summary)


async def _write(client: httpx.AsyncClient, summary: dict[str, Any]) -> str | None:
    experiment_id = await _experiment_id(client)
    if experiment_id is None:
        return None

    # Resolve each eval's trace before the run is written, so the ids are
    # available both for the run's tags and for the links the console renders.
    sessions = {r["session_id"] for r in summary["results"] if r.get("session_id")}
    by_session = await _await_traces(client, experiment_id, sessions)
    for r in summary["results"]:
        trace_id = by_session.get(r.get("session_id") or "")
        if trace_id:
            r["trace_id"] = trace_id
            r["traceUrl"] = (
                f"{MLFLOW_URL or MLFLOW_API_URL}/#/experiments/{experiment_id}"
                f"/traces?workspace={NAMESPACE}&selectedEvaluationId={trace_id}"
            )

    now_ms = int(time.time() * 1000)
    try:
        created = await client.post(
            _api("/runs/create"),
            json={
                "experiment_id": experiment_id,
                "start_time": now_ms,
                "tags": [
                    {"key": "mlflow.runName", "value": f"evals-{now_ms}"},
                    {"key": "kind", "value": "eval-sweep"},
                    {"key": "namespace", "value": NAMESPACE},
                ],
            },
            headers=_headers(),
            timeout=15.0,
        )
        created.raise_for_status()
        run_id = created.json()["run"]["info"]["run_id"]

        await _link_traces(
            client,
            run_id,
            [r["trace_id"] for r in summary["results"] if r.get("trace_id")],
        )

        metrics = [
            {
                "key": f"eval.{r['id'].replace('-', '_')}",
                "value": 1.0 if r["outcome"] == "passed" else 0.0,
                "timestamp": now_ms,
                "step": 0,
            }
            for r in summary["results"]
            if r["outcome"] in ("passed", "failed")
        ]
        metrics.append(
            {"key": "evals.passed", "value": float(summary["passed"]),
             "timestamp": now_ms, "step": 0}
        )
        metrics.append(
            {"key": "evals.total", "value": float(summary["total"]),
             "timestamp": now_ms, "step": 0}
        )
        tags = [
            {"key": f"inconclusive.{r['id']}", "value": r["detail"][:400]}
            for r in summary["results"]
            if r["outcome"] == "inconclusive"
        ]
        # Each eval's trace, also recorded on the run as a tag. The real
        # association is made by _link_traces below; these are here because a
        # tag is readable at a glance when browsing the run.
        tags += [
            {"key": f"trace.{r['id']}", "value": r["trace_id"]}
            for r in summary["results"]
            if r.get("trace_id")
        ]

        await client.post(
            _api("/runs/log-batch"),
            json={"run_id": run_id, "metrics": metrics, "tags": tags},
            headers=_headers(),
            timeout=20.0,
        )
        await client.post(
            _api("/runs/update"),
            json={"run_id": run_id, "status": "FINISHED", "end_time": int(time.time() * 1000)},
            headers=_headers(),
            timeout=15.0,
        )
        # Built from MLFLOW_URL, not MLFLOW_API_URL: this one is for a human
        # with a browser and an OpenShift session, not for this process.
        base = MLFLOW_URL or MLFLOW_API_URL
        return f"{base}/#/experiments/{experiment_id}/runs/{run_id}"
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("mlflow eval-sweep logging failed: %s", exc)
        return None
