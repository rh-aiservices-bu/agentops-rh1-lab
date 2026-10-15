"""Checking an operator's prompt against NeMo Guardrails before the agent sees it.

Off unless `NEMO_ENDPOINT` is set, which is the shipped state. The baseline lab
has no input guardrail at all, and that weakness is *absent infrastructure
config* rather than a flag in this application (implementation-plan.md D10) —
so there is no `GUARDRAILS_ENABLED`, and an unset endpoint costs not even a
token read.

Input only. Exactly one `user` message goes in each request, which is what
keeps the configured *output* rails out of it: `/v1/guardrail/checks` evaluates
whatever messages it is handed, so sending the assistant's reply as well would
quietly turn this into an output filter too.

Three things about the deployed instance are not guessable from the API docs,
and all three were read off the running `NemoGuardrails` in
`user-w562s-agentops` on cluster-pz7xk:

- **It is HTTPS with authentication, not a plain HTTP service.** The CR carries
  `security.opendatahub.io/enable-auth: "true"`, so the pod runs
  `kube-rbac-proxy` on 8443 behind Service port 443, and the certificate is
  issued by `openshift-service-serving-signer` — unknown to any public trust
  store. Hence both a bearer token and a CA file, defaulted to the two files
  the pod's ServiceAccount projection already contains.
- **Authorization is a SubjectAccessReview for a Service that does not exist.**
  `nemo-guardrails-rbac-proxy-config` asks for `get` on
  `services/nemo-guardrails-service`; the actual Service object is named
  `nemo-guardrails`. Nothing reconciles the two — RBAC matches the string the
  proxy was configured with, so the console's Role has to name
  `nemo-guardrails-service` or every check 403s.
- **`self check input` is a model call.** The `northgate` config judges the
  prompt with `qwen3-235b-a22b` through the shared MaaS endpoint, so a check is
  a round trip that can be slow and can 429 — not a regex. `detect sensitive
  data on input` is local and fast; the timeout here has to cover the former.

Nothing here may block a turn by accident. Only an explicit
`{"status": "blocked"}` blocks: every other outcome — no config, no token, 403,
a timeout, a non-JSON body, a body whose shape we do not recognise — fails
*open* and is reported to the participant as a check that did not run. A
guardrail that silently stops working must look different from one that
refused, or the first thing it teaches is distrust of the transcript.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import harness

#: The guardrails service. Empty — the shipped value — means no input check at
#: all. When set it is the HTTPS Service, e.g.
#: `https://nemo-guardrails.<user>-agentops.svc:443`; the instance is fronted
#: by kube-rbac-proxy and does not serve plain HTTP.
ENDPOINT = os.environ.get("NEMO_ENDPOINT", "").strip().rstrip("/")
#: Which rail set to apply, sent as `guardrails.config_id`. `northgate` is the
#: config `tenant-guardrails` ships and the instance's own default, so this is
#: belt-and-braces; empty omits the key and lets the server choose.
CONFIG_ID = os.environ.get("NEMO_CONFIG_ID", "northgate").strip()
#: The `model` field the request requires. The value does not pick the judge —
#: the rail config does that — but the field must be present.
MODEL = os.environ.get("NEMO_MODEL", "").strip() or harness.AGENT_MODEL
#: Generous, because `self check input` is an LLM round trip through an endpoint
#: that answers 429 under load. Still far below the agent's own timeout: a hung
#: guardrail may cost a turn its check, never the turn itself.
TIMEOUT_S = float(os.environ.get("NEMO_TIMEOUT_S", "20"))
#: Projected ServiceAccount token, read per check and never cached — the
#: projection rotates, and a cached copy starts 401ing hours into a workshop.
#: Defaulted to the path every pod already has.
TOKEN_FILE = os.environ.get(
    "NEMO_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"
).strip()
#: The service-serving CA. Also already in the pod, alongside the token, so the
#: common case needs no volume and no configuration. Falls back to the system
#: trust store, which is right for a route-based endpoint and wrong — visibly,
#: as a failed check rather than a silent allow — for the in-cluster Service.
_PROJECTED_CA = "/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt"
CA_FILE = os.environ.get("NEMO_CA_FILE", "").strip() or (
    _PROJECTED_CA if os.path.isfile(_PROJECTED_CA) else ""
)

CHECK_PATH = "/v1/guardrail/checks"

_client: httpx.AsyncClient | None = None


@dataclass(frozen=True)
class Verdict:
    """What the guardrail said, and whether it managed to say anything."""

    #: False only on an explicit block. Every failure allows.
    allowed: bool = True
    #: The rails that blocked, named as the service names them.
    rails: tuple[str, ...] = ()
    #: Did the check actually run? False both when no endpoint is configured
    #: and when the request failed — `error` distinguishes them.
    checked: bool = False
    #: Why the check did not run. Empty when it did, or when none was wanted.
    error: str = ""

    @property
    def summary(self) -> str:
        return ", ".join(self.rails) if self.rails else "no rail named"


def enabled() -> bool:
    """Is an input guardrail wired on this deployment?"""
    return bool(ENDPOINT)


def _client_for_nemo() -> httpx.AsyncClient:
    # Deliberately not the client `app.py` uses for the agent, for the same
    # reason `traces.py` keeps its own: that one may carry the OpenShell
    # gateway's client certificate and trust only its CA. `verify` is fixed per
    # client in httpx, so this cannot be a per-request argument either.
    global _client
    if _client is None:
        verify: Any = CA_FILE if CA_FILE else True
        _client = httpx.AsyncClient(verify=verify, timeout=TIMEOUT_S)
    return _client


def _token() -> str:
    try:
        return Path(TOKEN_FILE).read_text().strip()
    except OSError:
        return ""


def _blocked_rails(body: dict) -> tuple[str, ...]:
    """Which rails refused, from whichever field carries them.

    `rails_status` is the documented place and names every rail that ran, so
    the blocked ones have to be filtered out of it. `activated_rails` is the
    fallback: present in the same responses, and the only field left if a
    future version drops the per-rail status.
    """
    rails = [
        name
        for name, status in (body.get("rails_status") or {}).items()
        if isinstance(status, dict) and status.get("status") == "blocked"
    ]
    if rails:
        return tuple(rails)
    log = ((body.get("guardrails_data") or {}).get("log") or {})
    activated = log.get("activated_rails") or []
    return tuple(str(name) for name in activated if isinstance(name, (str, int)))


async def check_input(message: str) -> Verdict:
    """Ask the guardrail about one operator prompt.

    Never raises. An endpoint that is missing, unreachable, unauthorized, slow
    or incomprehensible yields an allowing verdict with `checked` false, and the
    caller is responsible for saying so on screen.
    """
    if not enabled():
        return Verdict()

    token = _token()
    if not token:
        return Verdict(error=f"no ServiceAccount token at {TOKEN_FILE}")

    payload: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": message}],
    }
    if CONFIG_ID:
        payload["guardrails"] = {"config_id": CONFIG_ID}

    try:
        response = await _client_for_nemo().post(
            f"{ENDPOINT}{CHECK_PATH}",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        return Verdict(error=f"guardrail unreachable: {exc}")

    if response.status_code >= 400:
        # 401/403 here is kube-rbac-proxy, not the rails: the console's
        # ServiceAccount is missing `get services/nemo-guardrails-service`.
        return Verdict(
            error=f"guardrail returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        body = response.json()
    except ValueError:
        return Verdict(error=f"guardrail returned a non-JSON body: {response.text[:200]}")
    if not isinstance(body, dict):
        return Verdict(error="guardrail returned an unrecognised body")

    status = body.get("status")
    if status == "blocked":
        return Verdict(allowed=False, rails=_blocked_rails(body), checked=True)
    if status in ("success", "allowed", "pass"):
        return Verdict(checked=True)
    # Not an error from the service, but not an answer either. Allowing while
    # saying so is the only honest option: inventing a block from a shape we do
    # not understand would be unexplainable to the participant it stops.
    return Verdict(error=f"guardrail returned an unrecognised status: {status!r}")


def blocked_events(verdict: Verdict) -> list[dict]:
    """The console frames for a prompt the guardrail refused.

    Reuses the status/error/done shapes the chat pane already renders, so a
    guardrail block reads like the gateway tool denials beside it — the denial
    names the component that refused it, rather than looking like the model
    having second thoughts.
    """
    return [
        {
            "type": "status",
            "level": "bad",
            "message": f"input blocked by NeMo Guardrails — {verdict.summary}",
        },
        {
            "type": "error",
            "detail": (
                "Blocked by the input guardrail before the agent saw it. "
                f"Rails that refused: {verdict.summary}."
            ),
        },
        {"type": "done", "blocked": True},
    ]


def notice_events(verdict: Verdict) -> list[dict]:
    """The frames for a check that was wanted and did not happen.

    Empty when there is nothing to say, so the caller can concatenate it
    unconditionally. A failed check is announced rather than logged: a
    guardrail that is quietly not running is worse than one that is absent,
    because the absence is at least visible in the deployment.
    """
    if not enabled() or verdict.checked or not verdict.error:
        return []
    return [
        {
            "type": "status",
            "level": "bad",
            "message": f"guardrail check did not run ({verdict.error}) — prompt sent unchecked",
        }
    ]
