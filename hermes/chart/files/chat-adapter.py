#!/usr/bin/env python3
"""Chat adapter: exposes Hermes over the waterplant-ui /chat + /chat/stream contract.

Runs INSIDE the OpenShell sandbox (started via plain `oc exec`, not `openshell
sandbox exec` — see below), driving `hermes -z "<message>"` (oneshot mode: one
turn, approvals auto-bypassed, prints the final response, exits) per request.

IMPORTANT — two different network namespaces are involved here, confirmed
live via /proc/*/ns/net inode comparison, not assumed:
  - This process itself runs in the sandbox pod's *default* network namespace
    (same one `oc exec`/the pod's primary container use) — that's the only
    namespace a Kubernetes Service can route to, so it has to live there to be
    reachable at all. OpenShell's own docs confirm it has no built-in inbound
    port-exposure mechanism (it's an egress-only sandbox by design), so this
    isn't a workaround for a missing feature — it's the intended split.
  - `openshell sandbox exec` (used below, NOT a direct `hermes` subprocess
    call) puts each invocation in its OWN separate, policy-enforced network
    namespace, proxied through the sandbox's Landlock-governed egress proxy.
    That's the one place `hermes -z`'s actual LLM/MCP/web calls need to run —
    calling `hermes` directly from this process's own namespace would bypass
    the network policy entirely (confirmed: an unlisted host was reachable
    when tested that way). See hermes/README.md.

stdlib only — no pip installs at runtime, so no PyPI network policy needed.

Routes:
    GET  /healthz       -> 200 {"status": "ok"}
    POST /chat          -> 200 {"reply": str, "steps": null, "rateLimitRetries": 0, "toolCalls": []}
    POST /chat/stream    -> SSE, frames matching agent/src/waterplant_agent/streaming.py's vocabulary:
                            {"type": "status"|"token"|"done"|"error", ...}

Known gap: oneshot mode returns only the final content block, not a
per-step trace, so /chat/stream emits exactly one status + one token frame
before done — no tool_call/tool_result events. See hermes/README.md.

Known gap: the inbound Authorization header is accepted but not used — Hermes
always calls MCP tools as its own fixed Keycloak service-account identity
(see mcp-token-refresh.py), never the caller's. See hermes/README.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("ADAPTER_PORT", "8080"))
TIMEOUT_S = float(os.environ.get("ADAPTER_TIMEOUT_S", "240"))
MAX_MESSAGE_CHARS = 4000
SANDBOX_NAME = os.environ.get("SANDBOX_NAME", "")
OPENSHELL_BIN = os.environ.get("OPENSHELL_BIN", "/sandbox/openshell")


def _log(*args: object) -> None:
    print(*args, file=sys.stderr, flush=True)


def _run_hermes_oneshot(message: str) -> tuple[int, str, str]:
    """Run `hermes -z <message>` through `openshell sandbox exec`, returning
    (returncode, stdout, stderr).

    Routing through `openshell sandbox exec` (rather than calling `hermes`
    directly, which would inherit this process's own unenforced network
    namespace) is what puts the actual LLM/MCP/web call through the sandbox's
    Landlock-governed egress proxy — see the module docstring.

    Raises subprocess.TimeoutExpired on timeout — caller handles it.
    """
    if not SANDBOX_NAME:
        raise RuntimeError("SANDBOX_NAME is not set — required to route through openshell sandbox exec")
    proc = subprocess.run(
        [OPENSHELL_BIN, "sandbox", "exec", "--name", SANDBOX_NAME, "--", "hermes", "-z", message],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        env=os.environ.copy(),
    )
    return proc.returncode, proc.stdout, proc.stderr


class ChatError(Exception):
    def __init__(self, status: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


def _handle_chat(message: str) -> dict:
    """Run one oneshot turn and return the /chat response body, or raise ChatError."""
    if not message or not message.strip():
        raise ChatError(400, "invalid_request", "message must be non-empty")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ChatError(
            400, "invalid_request", f"message exceeds {MAX_MESSAGE_CHARS} chars"
        )

    try:
        code, stdout, stderr = _run_hermes_oneshot(message)
    except subprocess.TimeoutExpired:
        raise ChatError(
            504, "timeout", f"hermes did not respond within {TIMEOUT_S}s"
        )
    except Exception as exc:  # noqa: BLE001 - never let this escape the handler
        raise ChatError(500, "internal_error", str(exc))

    if code != 0:
        raise ChatError(
            502, "agent_error", (stderr or f"hermes exited {code}").strip()[:2000]
        )

    return {
        "reply": stdout.strip(),
        "steps": None,
        "rateLimitRetries": 0,
        "toolCalls": [],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "hermes-chat-adapter/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        _log("%s - %s" % (self.address_string(), fmt % args))

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ChatError(400, "invalid_request", "body is not valid JSON")
        if not isinstance(data, dict):
            raise ChatError(400, "invalid_request", "body must be a JSON object")
        return data

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not_found", "detail": self.path})

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        if self.path == "/chat":
            self._do_chat()
        elif self.path == "/chat/stream":
            self._do_chat_stream()
        else:
            self._send_json(404, {"error": "not_found", "detail": self.path})

    def _do_chat(self) -> None:
        try:
            body = self._read_json_body()
            message = str(body.get("message", ""))
            result = _handle_chat(message)
        except ChatError as exc:
            self._send_json(exc.status, {"error": exc.error, "detail": exc.detail})
            return
        self._send_json(200, result)

    def _sse_frame(self, payload: dict) -> bytes:
        return ("data: " + json.dumps(payload) + "\n\n").encode()

    def _do_chat_stream(self) -> None:
        try:
            body = self._read_json_body()
            message = str(body.get("message", ""))
        except ChatError as exc:
            self._send_json(exc.status, {"error": exc.error, "detail": exc.detail})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        self.wfile.write(
            self._sse_frame({"type": "status", "message": "running hermes -z"})
        )
        self.wfile.flush()

        try:
            result = _handle_chat(message)
        except ChatError as exc:
            self.wfile.write(self._sse_frame({"type": "error", "detail": exc.detail}))
            self.wfile.flush()
            return

        self.wfile.write(
            self._sse_frame({"type": "token", "text": result["reply"]})
        )
        self.wfile.write(
            self._sse_frame({"type": "done", "steps": 0, "rateLimitRetries": 0})
        )
        self.wfile.flush()


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    _log(f"chat-adapter listening on :{PORT} (ADAPTER_TIMEOUT_S={TIMEOUT_S})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
