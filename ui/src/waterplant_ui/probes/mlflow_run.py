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
