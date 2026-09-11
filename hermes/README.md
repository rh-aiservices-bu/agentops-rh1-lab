# hermes/ — Hermes agent, OpenShell-sandboxed

A second AI agent for this lab, alongside `agent/`. Where `agent/` is a
purpose-built agent with an explicit tool-authorization contract (D8:
propagates the caller's token, makes zero authz decisions itself), Hermes is
an existing general-purpose CLI agent ([RHRolun/hermes-agent](https://github.com/RHRolun/hermes-agent),
a fork of NousResearch's Hermes) run inside an [OpenShell](https://github.com/nvidia/openshell)
sandbox — OpenShell enforces a Landlock filesystem/network policy around
whatever Hermes tries to do, which is the point: it's the "how do you govern
an agent you don't control the source of" half of the lab, as opposed to
`agent/`'s "how do you build one safely from scratch" half.

This deployment is independent of the existing Hermes-OpenShell deployment in
the `cai-crew` namespace (a different team's setup) — everything here is
self-contained in `wp-dev` and reproducible from this repo alone.

## Why there's a Containerfile here but no BuildConfig

`hermes/Containerfile` is kept for provenance/reproducibility, not built by
this repo's `deploy/apps/kustomization.yaml` (unlike `plant-api`, the MCP
servers, and `waterplant-ui`, which all have BuildConfigs). The image is
built and pushed manually (currently `quay.io/rlundber/agentops-rh1-hermes:0.1`
— check `hermes/chart/values.yaml`'s `image.*` for what's actually deployed)
and everything this deployment adds on top (MCP wiring, trimmed config,
sandbox policy) is injected at runtime onto the sandbox's `/sandbox` PVC, not
baked into the image. Rebuild only if you need to bump a baked-in dependency
(Python version, `hermes-agent` fork commit, the pinned `mcp`/`starlette`
versions, etc.) — see the Containerfile's own build command.

## How this deployment exposes Hermes: its own native `api_server`, not a custom adapter

This deployment runs `hermes gateway run` with Hermes's own built-in
OpenAI-compatible `api_server` platform enabled (`gateway/platforms/api_server.py`
upstream) — a persistent process exposing `/v1/chat/completions`, `/health`,
etc. `ui/src/waterplant_ui/harness.py`'s `AGENT_PROTOCOL=openai` path talks to
it directly; no custom adapter code is needed on the Hermes side. A single
`mcp-token-refresh.py` (stdlib-only Python, no pip installs needed at
runtime) keeps Hermes's MCP Gateway OAuth token fresh by periodically
fetching one via Keycloak client_credentials and writing it to
`$HERMES_HOME/mcp-tokens/gateway.json`, which Hermes watches for changes.

**The tradeoff this makes, deliberately, not accidentally:** for
`hermes gateway run` to be reachable via the Kubernetes Service at all, it
has to run in the sandbox pod's *default* network namespace (confirmed via
`/proc/*/ns/net` inode comparison — `openshell sandbox exec` runs each
invocation in a separate, policy-enforced namespace that a Service cannot
route to; OpenShell's own docs confirm it has no inbound-exposure mechanism).
That means Hermes's own LLM/MCP/web calls are **not currently subject to
`openshellPolicy`'s network allowlist** in this build — a real, known gap,
not silently missing. An earlier design (preserved at `../hermes-tmp/`) drove
per-turn `hermes -z` oneshot calls through `openshell sandbox exec` instead,
which *did* keep every call individually policy-enforced, at the cost of
Hermes's native streaming/session/API-server features (that design used a
custom `chat-adapter.py` shim and had its own gaps — no true token streaming,
no per-user identity passthrough, no tool-call trace — see
`hermes-tmp/README.md`). Revisit that tradeoff if real network-policy
enforcement for Hermes's own calls becomes a hard requirement.

## Known functional gaps (accepted tradeoffs, not bugs)

- **Network-policy enforcement does not currently apply to Hermes's own
  calls** — see above. `openshellPolicy`'s filesystem restrictions may still
  apply pod-wide regardless of network namespace (genuinely unconfirmed,
  worth testing directly rather than assuming either way).
- **No per-user attribution.** Hermes's `api_server` platform authenticates
  callers with a single static `API_SERVER_KEY` (per participant, but still
  one shared key, not the individual caller's own Keycloak token) — the
  console's `ui/harness.py` explicitly reports this via `/api/config`'s
  `identityPropagated: false` rather than silently missing it. Hermes's own
  MCP calls run as the fixed `wp-dev/hermes-agent` Keycloak service identity
  (full tool access, including `set_pump_speed`, `emergency_shutdown`,
  `dump_plant_configuration`), so every chat through the UI can trigger any
  tool regardless of who's asking.
- **No tool-call trace.** Hermes's agent loop runs server-side inside
  `hermes gateway run`; only the finished answer crosses the wire, so the
  console's `agentTraceAvailable` reads `false` for this path.
- **Single-tenant UI, per-participant backends.** `waterplant-ui` has one
  `AGENT_URL`; with Hermes provisioned per-participant, only one
  participant's Hermes can be the live `AGENT_URL` target at a time. See
  `deploy/README.md`'s Hermes section.

## Layout

```
hermes/
├── Containerfile           # provenance only — see above, not built by this repo
├── README.md                # this file
└── chart/                   # Helm chart — installs the shared OpenShell gateway.
    ├── values.yaml            # LLM/MCP endpoints, image ref, openshell policy toggles, persona
    ├── files/
    │   └── mcp-token-refresh.py  # source of truth for the in-sandbox token refresher
    └── templates/
        └── configmap-scripts.yaml   # embeds files/*.py + renders hermes-config.yaml.template
                                      # (model, mcp_servers, toolsets, platforms.api_server),
                                      # SOUL.md, and policy-standard.yaml.template from values.yaml
```

The script lives under `chart/files/` (not a separate directory) because
Helm's `.Files.Get` can only read files inside the chart directory. Per-participant
sandbox provisioning is driven by `deploy/scripts/add-participant.sh`, which
pulls the rendered ConfigMap this chart produces — see `deploy/README.md`.

## Teardown

```bash
helm uninstall hermes-openshell -n wp-dev   # removes the shared gateway
openshell sandbox delete hermes-<name>      # per participant
oc delete svc hermes-<name> -n wp-dev
```

The cluster-scoped `agent-sandbox` CRD/controller is shared with other
namespaces (e.g. `cai-crew`) — this deployment never installs or removes it,
only reuses what's already present.
