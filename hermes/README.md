# hermes/ — Hermes agent, OpenShell-sandboxed

A second AI agent for this lab, alongside `agent/`. Where `agent/` is a
purpose-built agent with an explicit tool-authorization contract (D8:
propagates the caller's token, makes zero authz decisions itself), Hermes is
an existing general-purpose CLI agent ([RHRolun/hermes-agent](https://github.com/RHRolun/hermes-agent),
a fork of NousResearch's Hermes — the user's own fork lives locally at
`cai-krew/hermes-agent`, sibling to this repo) run inside an
[OpenShell](https://github.com/nvidia/openshell) sandbox — OpenShell enforces
a Landlock filesystem/network policy around whatever Hermes tries to do,
which is the point: it's the "how do you govern an agent you don't control
the source of" half of the lab, as opposed to `agent/`'s "how do you build
one safely from scratch" half.

This deployment is independent of the existing Hermes-OpenShell deployment in
the `cai-crew` namespace (a different team's setup) — everything here is
self-contained in `wp-dev` and reproducible from this repo alone.

## Why there's a Containerfile here but no BuildConfig

`hermes/Containerfile` is kept for provenance/reproducibility, not built by
this repo's `deploy/apps/kustomization.yaml` (unlike `plant-api`, the MCP
servers, and `waterplant-ui`, which all have BuildConfigs). The image is
built and pushed manually — check `hermes/chart/values.yaml`'s `image.*` for
what's actually deployed, it will drift as the image gets rebuilt — and
everything this deployment adds on top (MCP wiring, trimmed config, sandbox
policy, the `hermes_otel` plugin) is injected at runtime onto the sandbox's
filesystem, not baked into the image. Rebuild only if you need to bump a
baked-in dependency, or when you change `hermes-agent`'s own source — see
the `_check_auth` and `mcp_tool.py` patches below, both of which require a
rebuild+push to take effect.

## Architecture: real Landlock enforcement AND real reachability, via the gateway's service relay

`hermes gateway run` (Hermes's own native OpenAI-compatible `api_server`
platform, `gateway/platforms/api_server.py` upstream) is started via
`openshell sandbox exec` — **not** plain `oc exec` — so the process runs
inside the sandbox's own Landlock-policy-enforced network namespace, not the
pod's default one. (These are two different network namespaces on the same
pod — confirmed via `/proc/*/ns/net` inode comparison — a process started
via plain `oc exec` is invisible to the relay described next.)

Reachability is via `openshell service expose <sandbox> <port> <name>`,
which relays gateway-side HTTPS traffic into that same network namespace's
loopback, over the sandbox's existing outbound supervisor connection — no
Kubernetes Service, no direct Pod-IP route needed. This is what earlier
investigation missed: OpenShell isn't purely egress-only, it has a real
(if narrowly-documented) ingress mechanism.

This is genuinely better than either of the two earlier designs (both still
present in the repo for reference — see "Superseded designs" below): it gets
Hermes's native streaming/session/API-server features (unlike the
`chat-adapter.py` design) **and** real per-call Landlock enforcement (unlike
the plain-`oc exec` design). Verified directly, repeatably:
a filesystem write to `/usr/` inside the sandbox fails with `Permission
denied`; a request to a non-allowlisted host (`example.com`) is blocked at
the egress proxy with `403`; a real `/v1/chat/completions` call through
`waterplant-ui` still succeeds and triggers genuine MCP tool calls.

### What this requires, and the two real bugs it took to get there

The OpenShell gateway must run with its default TLS/mTLS on
(`openshellGateway.enableTls: true` in `hermes/chart/values.yaml` — this is
the default; the chart used to install with TLS force-disabled for
simplicity before this architecture existed, see
`hermes/chart/templates/job-install-gateway.yaml`'s conditional `--set`
flags if you ever need to understand or revert that). With TLS on, **every**
caller of an exposed service — including `waterplant-ui` — must present a
valid mTLS client certificate (the `openshell-client-tls` Secret) or the TLS
handshake itself fails; `server.auth.allowUnauthenticatedUsers: true` does
**not** cover this, it's a transport-layer requirement (confirmed live: a
request with no client cert gets a TLS-level rejection, not an HTTP 401).

Two non-obvious bugs, both confirmed via source reading + live testing
(not guessed), had to be found and fixed to make this work:

1. **502 "Service endpoint is not reachable."** As noted above: starting the
   exposed process via plain `oc exec` puts it in the wrong network
   namespace relative to what the relay connects to. Fix: always start it
   via `openshell sandbox exec`.
2. **OpenShell's relay unconditionally strips the `Authorization` header**
   before forwarding to the sandboxed backend
   (`crates/openshell-server/src/service_routing.rs`'s
   `is_gateway_auth_header` — hardcoded upstream, no config flag). Hermes's
   `api_server` platform's `_check_auth` exclusively reads that same header,
   with no cookie/query-param/alternate-header fallback anywhere in the
   file, and no raw-TCP-tunnel alternative exists on the OpenShell side
   either (only HTTP `service expose`). A real, unconfigurable collision
   between the two systems.

   **Fix: a patch to the `hermes-agent` fork itself**
   (`cai-krew/hermes-agent/gateway/platforms/api_server.py`, `_check_auth`):
   if no `Authorization` header is present, the TCP peer (`request.remote` —
   the real transport-level source address, not a spoofable header) is
   loopback (`127.0.0.1`/`::1`), *and* `self._skip_auth` is true, auth
   passes — trusting that the gateway's mTLS layer already authenticated
   the caller before the relay ever reached this process. A non-loopback
   peer still always requires the bearer token, unchanged.

   `self._skip_auth` is config-gated, not unconditional (a later refinement
   over the first version of this patch): it reads
   `platforms.api_server.extra.skip_auth` from `config.yaml`, defaulting to
   off, so a plain deployment of this fork stays exactly as secure as before
   this patch existed. Wired on for this deployment in
   `hermes/chart/templates/configmap-scripts.yaml`'s
   `hermes-config.yaml.template` (needs the `extra:` nesting specifically —
   a plain top-level `platforms.api_server.skip_auth` is silently ignored,
   see the comment there for why).

A single `mcp-token-refresh.py` (stdlib-only Python, no pip installs needed
at runtime) keeps Hermes's MCP Gateway OAuth token fresh, run as a
background loop inside the sandbox (also via `sandbox exec`), writing to
`$HERMES_HOME/mcp-tokens/gateway.json`, which Hermes watches for changes.

### `waterplant-ui`'s side of this

`waterplant-ui` needs three things to reach Hermes through the relay, none
of which are in a committed manifest (the Deployment and Route were both
created imperatively — there is no YAML source of truth for either; redo
these by hand if either is ever recreated from scratch):

1. **mTLS client cert mounted as a volume**, from the `openshell-client-tls`
   Secret — `oc set volumes deployment/waterplant-ui --add --name=openshell-mtls
   --type=secret --secret-name=openshell-client-tls --mount-path=/etc/openshell-mtls --read-only=true`
2. **A `hostAliases` entry** mapping the relay's synthetic hostname
   (`default--<sandbox>--<name>.openshell.localhost` — not real DNS) to the
   `openshell` Service's ClusterIP — `oc patch deployment/waterplant-ui --type=json`
   adding `spec.template.spec.hostAliases`.
3. **Env vars**: `AGENT_URL` (the relay URL), `AGENT_PROTOCOL=openai`,
   `AGENT_TLS_CA` / `AGENT_TLS_CERT` / `AGENT_TLS_KEY` (paths under the
   mounted volume above). `AGENT_API_KEY` is still set but effectively does
   nothing on this path — the relay strips the header it would have gone in.

`ui/src/waterplant_ui/app.py`'s `client()` factory builds an explicit
`ssl.SSLContext` (`ssl.create_default_context(cafile=...)` +
`load_cert_chain(...)`) and passes it as `verify=` for exactly this reason:
**httpx 0.28.1's `verify=<ca path>` + `cert=(cert, key)` combination
silently fails to present the client cert** (confirmed live: identical files
via Python's raw `ssl` module handshake fine; httpx with the tuple form
gets `TLSV13_ALERT_CERTIFICATE_REQUIRED`). If httpx ever gets upgraded,
re-check whether this workaround is still needed before removing it.

### The Route needs a longer idle timeout, or long answers get cut off mid-stream

The OpenShift Router's default idle-connection timeout (~30s) will kill the
SSE connection to `waterplant-ui`'s Route whenever Hermes goes quiet for a
stretch — a slow LLM turn, a tool call — which happens routinely now that
there's an extra relay hop and (variably) a busy shared MaaS endpoint. This
presents as the chat pane silently losing the rest of an answer, easily
mistaken for "streaming doesn't work" (it does — verified independently at
every layer: Hermes's own SSE output, through the gateway relay, through
`httpx` exactly as the UI calls it, all deliver real incremental chunks).
Confirmed directly: `curl` against the real public Route died with
`transfer closed with outstanding read data remaining` right around the
default timeout, and stopped happening once a longer timeout was set.

**Fix, applied imperatively (also not in a committed manifest):**
```bash
oc annotate route waterplant-ui -n wp-dev haproxy.router.openshift.io/timeout=310s --overwrite
```
310s, not more, to stay just above `HTTP_TIMEOUT_S`'s existing 300s client-side
timeout in `app.py` — no point extending the Route past what the app itself
will already give up at.

### Streaming granularity (not a bug, just coarse)

Hermes doesn't emit fine-grained per-token deltas — it flushes in a handful
of large chunks tied to turn/tool-call boundaries (one empty
`delta:{"role":"assistant"}`, then tool-progress events if a tool runs, then
one or two big `delta:{"content": "..."}` chunks with many words each, then
`[DONE]`). A short answer with no tool call may arrive as a single chunk,
indistinguishable at a glance from non-streaming even though the SSE
mechanics are correct end-to-end. If real token-by-token streaming is
wanted, that's a change to `hermes-agent`'s own agent loop (where it decides
when to call `stream_delta_callback`), not anything in this repo.

### MCP tool-call timeout: a rejected/broken call used to hang for 300s

Calling an MCP tool that the gateway rejects (e.g. insufficient role) or a
genuinely broken tool on the shared MCP Gateway used to hang for the full
300s default timeout before failing — even though the gateway itself
rejects almost instantly. Root cause, confirmed by reading both the
`hermes-agent` source and the installed `mcp` SDK: the pending tool call's
future is scheduled independently of the transport's own task group
(`tools/mcp_tool.py`'s `_run_on_mcp_loop`, via
`asyncio.run_coroutine_threadsafe`), and the SDK's own per-request timeout
(`ClientSession`'s `read_timeout_seconds`) was never being set — so nothing
bounded the wait except Hermes's blunt outer watchdog.

Two-part fix, config value first with a real code fix behind it:
- `mcp_servers.<name>.timeout` in `config.yaml` (a real, already-documented
  knob) is set to `hermes/chart/values.yaml`'s new
  `mcp.toolTimeoutSeconds: 30` — caps the worst case at 30s instead of 300s
  on its own, no code change needed.
- `cai-krew/hermes-agent/tools/mcp_tool.py` now passes
  `read_timeout_seconds=timedelta(seconds=5)` into every `ClientSession(...)`
  construction, so the SDK's own `anyio.fail_after()` fires cleanly on its
  own with a proper `McpError` instead of relying on the blunt 30s cap.
  Deliberately set well below the 30s outer timeout — if the two are equal,
  the outer watchdog's synchronous poll wins the race almost every time
  and the SDK's cleaner error never actually surfaces (confirmed live: this
  is exactly what happened when both were first set to the same value).
  Requires a rebuild+push to take effect, like `_check_auth` above.

### Hermes's MCP identity: the realm's `operator`, not `wp-dev/hermes-agent`

Hermes's own MCP calls authenticate as the realm's `operator` user
(password grant via the public `mcp-gateway` client), not the
`wp-dev/hermes-agent` service-account client. `wp-dev/hermes-agent`'s
Keycloak role grant still mirrors full tool access (deliberately, from an
earlier decision), but using it here meant every chat through the UI could
trigger any tool — including `dump_plant_configuration`,
`emergency_shutdown` — regardless of which persona was selected in the UI,
since identity never reaches Hermes on this path anyway (see "no per-user
attribution" below). Using the operator's own credentials caps Hermes at
exactly the operator persona's tool scope instead — not real per-user
identity propagation, just a lower default ceiling. Wired in
`deploy/scripts/add-participant.sh`'s `hermes-mcp-auth-<name>` Secret
(`MCP_CLIENT_ID=mcp-gateway`, `MCP_USERNAME=operator`,
`MCP_PASSWORD=operator`) and `mcp-token-refresh.py` (password grant, not
client_credentials).

### MLflow tracing (one experiment per participant)

Hermes sends real OTLP traces to RHOAI's shared MLflow instance
(`https://rh-ai.apps.caiprod.rhoai.rh-aiservices-bu.com/mlflow`), via the
[`hermes_otel`](https://github.com/briancaffey/hermes-otel) plugin — a
GitHub-hosted plugin, not a pip package. The experiment is named after the
participant (`${NAME}`) and auto-created if missing (confirmed live:
`demo` → experiment 3, `alice` → experiment 4).

Installed **per-sandbox at provision time** (not baked into the image, even
though `hermes_cli.plugins.get_bundled_plugins_dir()` would allow that —
deliberately following the pattern both reference implementations in
`cai-krew/hermes-container` use, rather than a theoretically-cleaner
image-bake rework). `deploy/scripts/provision-hermes-sandbox.sh`
downloads the plugin's GitHub tarball via `curl`+`tar` (the setup Job's own
container, `quay.io/openshift/origin-cli:latest`, has neither `git` nor
`microdnf` — confirmed live) and extracts *only* its `hermes_otel/`
subdirectory directly (`--strip-components=2` from
`hermes-otel-main/hermes_otel/...`) — both upstream references' own
extraction scripts get this wrong (verified by downloading the actual
tarball: `plugin.yaml` is nested one level inside the package's own
`hermes_otel/` subdirectory, but their `--strip-components=1` only strips
the outer GitHub wrapper, which would double-nest the plugin if run today).

Auth: an `hermes-openshell-installer` ServiceAccount token (minted fresh
per provision run, `oc create token ... --duration=8760h`), authorized via
a `RoleBinding` (`hermes/chart/templates/rolebinding-mlflow.yaml`) to the
cluster-wide `mlflow-operator-mlflow-integration` ClusterRole. Baked as a
literal value into the plugin's own `config.yaml` at provision time — that
file does no runtime env-var expansion, so `${MLFLOW_SA_TOKEN}` in the
template is a `sed` placeholder, not something read live.
`X-MLflow-Workspace: wp-dev` is a real, working per-namespace data scope,
not decorative — confirmed live: a wp-dev-scoped search doesn't see
`cai-crew`'s own experiments, and each new experiment's own
`artifact_location` embeds the workspace path
(`mlflow-artifacts:/workspaces/wp-dev/<id>`).

## Tooling gotcha: `helm` and `oc` can disagree about which cluster is live

In this dev environment, `oc` is a WSL wrapper script that shells out to a
**Windows** `oc.exe`, which reads its own kubeconfig
(`/mnt/c/Users/<user>/.kube/config` from WSL's point of view) — separate
from `helm` (a native Linux binary)'s default `~/.kube/config`, which can be
stale. If `helm` fails with a DNS-lookup error against a cluster you don't
recognize, this is why —
`export KUBECONFIG=/mnt/c/Users/<user>/.kube/config` before any `helm`
command fixes it. `oc` itself needs no such override.

## Known functional gaps (accepted tradeoffs, not bugs)

- **No per-user attribution.** Hermes's `api_server` platform authenticates
  callers with a single static `API_SERVER_KEY` per participant (unused on
  the relay path specifically, see above) — the console's `ui/harness.py`
  explicitly reports this via `/api/config`'s `identityPropagated: false`
  rather than silently missing it. Hermes's own MCP calls now run as the
  realm's fixed `operator` identity (see "Hermes's MCP identity" above) —
  capped at the operator's own tool scope rather than full admin access,
  but still one shared identity for every chat through the UI, not real
  per-caller attribution.
- **No tool-call trace.** Hermes's agent loop runs server-side inside
  `hermes gateway run`; only the finished answer crosses the wire, so the
  console's `agentTraceAvailable` reads `false` for this path.
- **Single-tenant UI, per-participant backends.** `waterplant-ui` has one
  `AGENT_URL`; with Hermes provisioned per-participant, only one
  participant's Hermes can be the live target at a time.
- **`values.yaml`'s `llm.model` can drift from what's actually deployed.**
  It currently says `qwen3-235b`, but the live `provision.env` ConfigMap and
  the running sandbox's env both say `gemini-2.5-pro` — someone set that
  live via `helm upgrade --set llm.model=...` without updating the checked-in
  default. A plain `helm upgrade` with no override would silently switch the
  running model back. Reconcile one way or the other before relying on
  `values.yaml` as ground truth for what's live — always check the live
  ConfigMap (`oc get configmap hermes-openshell-scripts -n wp-dev -o
  jsonpath='{.data.provision\.env}'`) instead.

## Which Hermes config files exist, and where they actually live

| What | Template source (this repo) | Rendered into | Actually read from (inside the sandbox) |
|---|---|---|---|
| Main config (LLM provider, toolsets, MCP servers, `platforms.api_server`) | `chart/templates/configmap-scripts.yaml`'s `hermes-config.yaml.template`, filled from `chart/values.yaml` | `hermes-openshell-scripts` ConfigMap | `/sandbox/.hermes/config.yaml` |
| Persona/system prompt | `values.yaml`'s `persona.soul` | same ConfigMap | `/sandbox/.hermes/SOUL.md` |
| Env bootstrap (API keys, MCP creds, `API_SERVER_*`) | heredoc in `deploy/scripts/provision-hermes-sandbox.sh`, filled from the `hermes-mcp-auth-<name>` Secret + `provision.env` | rendered at provision time | `/sandbox/.sandbox-init.sh` (sourced before `hermes gateway run` starts) |
| MCP OAuth token cache | written by `mcp-token-refresh.py` | — | `/sandbox/.hermes/mcp-tokens/gateway.json` (watched by Hermes's `MCPOAuthManager` via file mtime) |
| Landlock policy (separate from Hermes's own app config) | `configmap-scripts.yaml`'s `policy-standard.yaml.template`, filled from `values.yaml`'s `openshellPolicy` | same ConfigMap | applied via `openshell policy set`, enforced by the sandbox supervisor — not a file Hermes itself reads |
| `hermes_otel` plugin's own MLflow config | `configmap-scripts.yaml`'s `hermes-otel-config.yaml.template`, filled from `values.yaml`'s `mlflow.*` | same ConfigMap | `/sandbox/.hermes/plugins/hermes_otel/config.yaml` (a different path from the main `config.yaml` — the plugin's files themselves are also uploaded here, downloaded fresh per sandbox, see "MLflow tracing" above) |

## Layout

```
hermes/
├── Containerfile           # provenance only — see above, not built by this repo
├── README.md                # this file
└── chart/                   # Helm chart — installs the shared OpenShell gateway.
    ├── values.yaml            # LLM/MCP endpoints, image ref, openshell/mlflow policy toggles, persona
    ├── files/
    │   └── mcp-token-refresh.py  # source of truth for the in-sandbox token refresher
    └── templates/
        ├── configmap-scripts.yaml   # embeds files/*.py + renders hermes-config.yaml.template
        │                             # (model, mcp_servers, toolsets, platforms.api_server, plugins),
        │                             # SOUL.md, hermes-otel-config.yaml.template, and
        │                             # policy-standard.yaml.template from values.yaml
        └── rolebinding-mlflow.yaml   # authorizes hermes-openshell-installer against RHOAI MLflow
```

The script lives under `chart/files/` (not a separate directory) because
Helm's `.Files.Get` can only read files inside the chart directory. Per-participant
sandbox provisioning is driven by `deploy/scripts/provision-hermes-sandbox.sh`
(called by `deploy/scripts/add-participant.sh`), which pulls the rendered
ConfigMap this chart produces, starts `hermes gateway run` via
`openshell sandbox exec`, exposes it via `openshell service expose`, and
self-verifies the relay end-to-end before declaring done — see
`deploy/README.md`.

## Superseded designs (kept for reference, not deployed)

- **`../hermes-tmp/`** — custom `chat-adapter.py` driving per-turn `hermes -z`
  oneshot calls through `openshell sandbox exec`. Real enforcement, but no
  streaming, no per-user identity passthrough, no tool trace, more code to
  maintain. Superseded by the current design, which gets enforcement without
  those costs.
- **Plain-`oc exec` startup** (no longer present, was briefly deployed
  between the two designs above) — `hermes gateway run` reachable via a
  plain Kubernetes Service, but with **zero** Landlock enforcement, since
  running in the pod's default network namespace bypasses it entirely.
  Superseded by the `sandbox exec` + `service expose` architecture above,
  which is strictly better and carries no such tradeoff.

## Teardown

```bash
helm uninstall hermes-openshell -n wp-dev   # removes the shared gateway
openshell sandbox delete hermes-<name>      # per participant
openshell service delete hermes-<name> openai   # per participant, if not already gone with the sandbox
```

The cluster-scoped `agent-sandbox` CRD/controller is shared with other
namespaces (e.g. `cai-crew`) — this deployment never installs or removes it,
only reuses what's already present.
