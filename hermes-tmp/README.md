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
already built and pushed to `quay.io/rlundber/hermes-openshell:0.8` (a public
quay.io repo — no `imagePullSecret` needed) and everything this deployment
adds on top (chat adapter, MCP wiring, trimmed config, sandbox policy) is
injected at runtime onto the sandbox's `/sandbox` PVC, not baked into the
image. Rebuild only if you need to bump a baked-in dependency (Python
version, `hermes-agent` fork commit, etc.) — see the Containerfile's own
build command.

## Why Hermes doesn't run `hermes gateway run`

The upstream image supports Slack/Mattermost via `hermes gateway run`, but
this deployment doesn't start it — the custom `ui/` replaces Slack as the
chat surface, so no messaging platform is configured. Instead, two small
stdlib-only Python processes run inside the sandbox:

- `chat-adapter.py` — an HTTP server implementing the `POST /chat` and
  `POST /chat/stream` contract `ui/src/waterplant_ui/app.py` expects from
  `AGENT_URL`, by shelling out to `hermes -z "<message>"` (oneshot mode: one
  turn, approvals auto-bypassed, prints the final response, exits) per
  request.
- `mcp-token-refresh.py` — keeps Hermes's MCP Gateway OAuth token fresh by
  periodically fetching one via Keycloak client_credentials and writing it to
  `$HERMES_HOME/mcp-tokens/gateway.json`, which Hermes watches for changes.

Neither needs anything beyond the Python stdlib, so no PyPI network policy
grant is needed at runtime.

## Known functional gaps (accepted tradeoffs, not bugs)

- **No true token streaming.** `hermes -z` returns only the final response,
  not incremental output, so `/chat/stream` emits one `status` frame, one
  `token` frame with the full reply, then `done` — no live token-by-token
  rendering, no `tool_call`/`tool_result` trace events (`agent/`'s streaming
  contract supports both; Hermes's adapter only emits the subset oneshot mode
  can actually produce).
- **No per-user attribution.** The adapter accepts but does not use the
  caller's `Authorization` header — Hermes always calls MCP tools as its own
  fixed Keycloak service-account identity (`wp-dev/hermes-agent`, one per
  participant realm), never the individual user's. Combined with this
  identity holding full tool access (including `set_pump_speed`,
  `emergency_shutdown`, `dump_plant_configuration` — see
  `deploy/keycloak/realms/waterplant-realm-template.yaml`), every chat
  through the UI can trigger any tool regardless of who's asking, attributed
  to the shared Hermes identity rather than the individual participant. This
  is an intentional scope reduction for this lab component, not an
  oversight — flag it if you extend this toward anything beyond a lab.
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
    │   ├── chat-adapter.py       # source of truth for the in-sandbox HTTP shim
    │   └── mcp-token-refresh.py  # source of truth for the in-sandbox token refresher
    └── templates/
        └── configmap-scripts.yaml   # embeds files/*.py + renders hermes-config.yaml.template,
                                      # SOUL.md, and policy-standard.yaml.template from values.yaml

The scripts live under chart/files/ (not a separate adapter/ directory)
because Helm's `.Files.Get` can only read files inside the chart directory —
keeping one copy there avoids a second copy that could drift out of sync.
Per-participant sandbox provisioning is driven by
deploy/scripts/add-participant.sh, which pulls the rendered ConfigMap this
chart produces — see deploy/README.md.
```

## Teardown

```bash
helm uninstall hermes-openshell -n wp-dev   # removes the shared gateway
openshell sandbox delete hermes-<name>      # per participant
oc delete svc hermes-<name> -n wp-dev
```

The cluster-scoped `agent-sandbox` CRD/controller is shared with other
namespaces (e.g. `cai-crew`) — this deployment never installs or removes it,
only reuses what's already present.
