# Water Plant AgentOps Lab — Deployment Guide

This directory contains everything needed to stand up the Water Plant lab environment
on an OpenShift cluster.  Steps are ordered; each section depends on the previous one.

## Prerequisites

- `oc` logged in with cluster-admin (or sufficient rights to create projects and patch
  resources in `mcp-gateway`)
- MCP Gateway operator already installed on the cluster (the lab assumes a shared cluster
  where this is pre-provisioned)
- Kuadrant and Authorino already running in the `mcp-gateway` namespace
- The source directories `plant-api/`, `mcp-telemetry/`, `mcp-maintenance/`,
  `mcp-control/`, and `ui/` present at the repo root (they contain the Containerfiles)

---

## Step 1 — Create the project

```bash
oc new-project wp-dev
```

---

## Step 2 — Build and deploy the applications

The five app components are built on-cluster from local source using binary builds.

### 2a. Apply the BuildConfigs

```bash
oc apply -k deploy/apps/
```

### 2b. Build each image

Run from the repo root.  Each command uploads the local directory to the cluster builder
and streams the build log.

```bash
oc start-build plant-api       --from-dir=plant-api/       -n wp-dev --follow
oc start-build telemetry-mcp   --from-dir=mcp-telemetry/   -n wp-dev --follow
oc start-build maintenance-mcp --from-dir=mcp-maintenance/ -n wp-dev --follow
oc start-build control-mcp     --from-dir=mcp-control/     -n wp-dev --follow
oc start-build waterplant-ui   --from-dir=ui/              -n wp-dev --follow
```

> **Note:** Deployments, Services, and Routes for these apps are not captured in this
> repo.  On a fresh cluster they must be created separately (e.g. via `oc new-app` or
> by applying manifests from the GitOps repo).

### 2c. Verify

```bash
oc get pods -n wp-dev
# All five pods should be Running: plant-api, telemetry-mcp, maintenance-mcp,
# control-mcp, waterplant-ui
```

---

## Step 3 — Deploy Keycloak

### 3a. Install the RHBK operator

```bash
oc apply -k deploy/keycloak/operator/

# Wait for the operator to be ready (up to 10 min on first install)
oc wait csv -n wp-dev \
  -l operators.coreos.com/rhbk-operator.wp-dev="" \
  --for=jsonpath='{.status.phase}'=Succeeded \
  --timeout=600s
```

### 3b. Deploy PostgreSQL, Keycloak, and the route

```bash
oc apply -k deploy/keycloak/keycloak/

# Wait for PostgreSQL
oc wait pod -n wp-dev -l app=keycloak-pgsql \
  --for=condition=Ready --timeout=120s

# Wait for Keycloak itself (can take 2–3 min)
oc wait keycloak/keycloak -n wp-dev \
  --for=condition=Ready --timeout=300s
```

### 3c. Verify

```bash
KEYCLOAK_HOST=$(oc get route keycloak -n wp-dev -o jsonpath='{.spec.host}')
echo "Keycloak URL: https://${KEYCLOAK_HOST}"

# Admin credentials are in a generated secret
echo "Admin user:     $(oc get secret keycloak-initial-admin -n wp-dev -o jsonpath='{.data.username}' | base64 -d)"
echo "Admin password: $(oc get secret keycloak-initial-admin -n wp-dev -o jsonpath='{.data.password}' | base64 -d)"
```

---

## Step 4 — Register MCP servers with the MCP Gateway

Creates HTTPRoutes (exposing each MCP server to the gateway) and
MCPServerRegistrations (telling the broker about each server and its tool prefix).

```bash
oc apply -k deploy/mcp-gateway/
```

### Verify

```bash
oc get mcpserverregistration -n wp-dev
# Expected output — all three Ready with correct tool counts:
#   control-mcp     control_     ... True  7  ["control"]
#   maintenance-mcp maintenance_ ... True  5  ["maintenance"]
#   telemetry-mcp   telemetry_   ... True  6  ["telemetry"]
```

---

## Step 5 — Auth and authz

### 5a. Gateway-level auth (shared cluster)

> **Context:** On a shared cluster the `mcp-auth-policy` in the `mcp-gateway` namespace
> already exists and is shared with other lab groups.  The file `deploy/auth/authpolicy-auth.yaml`
> is a **reference document** showing the intended structure — do not apply it directly as
> that would overwrite the shared policy.
>
> On a **fresh standalone cluster** with no existing policy, apply it once:
> ```bash
> oc apply -f deploy/auth/kuadrant.yaml
> oc apply -f deploy/auth/envoyfilter-auth-ssl.yaml   # optional, only if Authorino needs TLS
> oc apply -f deploy/auth/authpolicy-auth.yaml
> # Then patch the MCPGatewayExtension using the instructions in
> # deploy/auth/mcpgatewayextension-oauth-patch.yaml
> ```
>
> Per-participant JWT issuers are added automatically by `add-participant.sh` (Step 6).

### 5b. Per-route tool-level authz (wp-dev owned)

Apply the three per-route AuthPolicies — one per MCP server — to the `wp-dev` namespace.
These are fully owned by this lab and are safe to apply at any time:

```bash
oc apply -k deploy/authz/
```

Wait for all three to be enforced:

```bash
oc get authpolicy -n wp-dev
# Expected: telemetry-mcp-authz, maintenance-mcp-authz, control-mcp-authz
#           all showing ENFORCED = true
```

Each policy:
- Allows unauthenticated access for MCP protocol messages (no `x-mcp-toolname` header)
- Requires a valid JWT with the correct `tool:<toolname>` role for every tool call
- Returns a structured JSON-RPC error on authorization failure

Per-participant JWT issuers are patched into each policy by `add-participant.sh` (Step 6).

---

## Step 6 — Add lab participants

Each participant gets their own isolated Keycloak realm containing an `operator` user
whose tool access can be configured independently.

```bash
./deploy/scripts/add-participant.sh <participant-name>

# Examples:
./deploy/scripts/add-participant.sh alice
./deploy/scripts/add-participant.sh bob
```

The script:
1. Renders `deploy/keycloak/realms/waterplant-realm-template.yaml` with the
   participant name (and a fresh `wp-dev/hermes-agent` client secret) and applies it
2. Polls until the realm import completes
3. Patches `mcp-auth-policy` to accept JWTs from the new realm (public listener)
4. Patches `telemetry-mcp-authz` to validate tool-call JWTs from the new realm
5. Patches `maintenance-mcp-authz` to validate tool-call JWTs from the new realm
6. Patches `control-mcp-authz` to validate tool-call JWTs from the new realm
7. Appends the realm's issuer URL to the `MCPGatewayExtension` authorization server list
8. Creates a `<name>-admin`/`redhat` realm-admin user in the new realm
9. If the `hermes-openshell` chart is installed (Step 7 below): provisions this
   participant's Hermes-OpenShell sandbox

The Keycloak hostname is derived automatically from the live route — no hardcoded values.

### What each participant gets

| Persona | Username | Password | Access |
|---------|----------|----------|--------|
| Operator | `operator` | `operator` | All telemetry + maintenance tools; safe control tools |

**Operator tool access:**

| Server | Tools granted | Tools withheld |
|--------|--------------|----------------|
| `telemetry-mcp` | All 6 (safety status, pump status, all-pump status, water quality, reservoir level, valve positions) | — |
| `maintenance-mcp` | All 5 (search history, get record, list/create/update work orders) | — |
| `control-mcp` | set_pump_speed, start_pump, stop_pump, open_valve, close_valve | `dump_plant_configuration` (Scenario 3), `emergency_shutdown` (Scenario 4) |

Participants can log into the Keycloak admin console for their realm to add or remove
tool roles as part of the lab exercises:

```
https://<keycloak-host>/admin/waterplant-<name>/console
```

---

## Step 7 — Deploy Hermes (OpenShell agent) — optional

A second AI agent for the lab, alongside `agent/`: Hermes, run inside an
[OpenShell](https://github.com/nvidia/openshell) sandbox. It runs
`hermes gateway run` with its built-in OpenAI-compatible `api_server`
platform, which `ui/src/waterplant_ui/harness.py`'s `AGENT_PROTOCOL=openai`
path talks to directly — see [`hermes/README.md`](../hermes/README.md) for
the full design, including a known, deliberate tradeoff this build accepts:
Hermes's own LLM/MCP/web calls run in the sandbox pod's default network
namespace (so the Service can reach the api_server at all), not through
`openshell sandbox exec`'s policy-enforced one — network-policy enforcement
is not currently in effect for this path. An earlier, retired design
(`hermes-tmp/`) drove per-turn `hermes -z` oneshot calls through
`openshell sandbox exec` instead, which did keep network calls
policy-enforced but couldn't reuse Hermes's own streaming/session/API-server
features — see `hermes-tmp/README.md` if reviving that tradeoff is ever worth it.

This step is optional and independent of Steps 1–6 — skip it if you only need
the core telemetry/maintenance/control lab.

### 7a. Provision an LLM key Secret (reuses `agent/`'s existing convention)

```bash
oc create secret generic litellm-key -n wp-dev \
  --from-literal=LITELLM_VIRTUAL_KEY=<key> --dry-run=client -o yaml | oc apply -f -
```

If `agent/` is already deployed, reuse its existing Secret name instead of
creating a new one — see `hermes/chart/values.yaml`'s `llm.apiKeySecretRef`
comment for the shared-fate tradeoff (both agents draw from the same
rate-limited MaaS quota).

### 7b. Install the shared OpenShell gateway

One `helm install` per cluster — installs a dedicated OpenShell gateway in
`wp-dev` (independent of any other OpenShell deployment elsewhere on the
cluster) and renders the config/policy ConfigMap that
`add-participant.sh` reads for each participant's sandbox:

```bash
helm install hermes-openshell hermes/chart -n wp-dev \
  --set llm.apiKeySecretRef.name=litellm-key
oc logs -f job/hermes-openshell-install-gateway -n wp-dev
```

Tune what the OpenShell sandbox policy allows/blocks via
`hermes/chart/values.yaml`'s `openshellPolicy.*` (network egress allowlist
toggles + an `extraNetworkPolicies` escape hatch, filesystem read-only/read-write
lists) — pass overrides with `-f my-overrides.yaml` or `--set`.

### 7c. Provision Hermes for each participant

Already wired into `add-participant.sh` — if the chart above is installed
before you run it, each participant automatically gets their own Hermes
sandbox, Keycloak `wp-dev/hermes-agent` client (full tool access — see
`hermes/README.md`), and Service. Re-run `add-participant.sh <name>` for a
participant created before you installed the chart to provision Hermes for
them retroactively.

### 7d. Point the UI at one participant's Hermes

`waterplant-ui` has a single `AGENT_URL` — with Hermes provisioned
per-participant, only one can be the live target at a time. Requires
`AGENT_PROTOCOL=openai` and the participant's `API_SERVER_KEY` (printed at
the end of `provision-hermes-sandbox.sh`'s output, also readable from the
`hermes-mcp-auth-<name>` Secret):

```bash
oc set env deployment/waterplant-ui -n wp-dev \
  AGENT_URL=http://hermes-<name>.wp-dev.svc:8080 \
  AGENT_PROTOCOL=openai \
  AGENT_API_KEY=<the API_SERVER_KEY printed by provision-hermes-sandbox.sh>
```

### Verify

```bash
oc get sandbox default--hermes-<name> -n wp-dev
oc get pods -n wp-dev -l app.kubernetes.io/name=hermes-sandbox,app.kubernetes.io/instance=<name>
oc get svc hermes-<name> -n wp-dev
oc run curl-test --rm -it --restart=Never -n wp-dev --image=registry.access.redhat.com/ubi9/ubi-minimal \
  -- curl -s http://hermes-<name>.wp-dev.svc:8080/health
```

---

## Testing

### Test 1 — Unauthenticated request is rejected

```bash
MCP_URL="https://mcp.apps.caiprod.rhoai.rh-aiservices-bu.com/mcp"
curl -sk -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  -w "\nHTTP %{http_code}" | tail -3
# Expected: HTTP 401  {"error":"Unauthorized",...}
```

### Test 2 — Get a token and list tools

```bash
KEYCLOAK_HOST=$(oc get route keycloak -n wp-dev -o jsonpath='{.spec.host}')
MCP_URL="https://mcp.apps.caiprod.rhoai.rh-aiservices-bu.com/mcp"

TOKEN=$(curl -sk \
  "https://${KEYCLOAK_HOST}/realms/waterplant-<name>/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=mcp-gateway&username=operator&password=operator" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Initialize a session
SESSION_ID=$(curl -ski -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
  | grep -i "mcp-session-id" | awk '{print $2}' | tr -d '\r')

# List tools — should show 18 wp-dev tools (telemetry_, maintenance_, control_)
curl -sk -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 -c "
import sys, json
tools = json.load(sys.stdin)['result']['tools']
wp = [t['name'] for t in tools if any(p in t['name'] for p in ['telemetry_','maintenance_','control_'])]
print(f'Total: {len(tools)} tools, wp-dev: {len(wp)}')
for t in sorted(wp): print(f'  {t}')
"
```

### Test 3 — Call a tool

```bash
curl -sk -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"telemetry_get_plant_safety_status","arguments":{}}}' \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
for c in d['result']['content']:
    if c['type'] == 'text': print(c['text'])
"
# Expected: plant safety status JSON — Pump 4 critical (8.3 mm/s vibration)
```

### Test 4 — Verify withheld tools are blocked (after authz is active)

```bash
# dump_plant_configuration is not in the operator's roles — should return 403
curl -sk -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"control_dump_plant_configuration","arguments":{}}}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin))"
# Expected: {"error":{"code":-32600,"message":"Forbidden: Insufficient permissions..."}}
```

---

## Directory reference

```
deploy/
├── apps/                          # Binary BuildConfigs for the five app components
│   ├── plant-api-buildconfig.yaml
│   ├── telemetry-mcp-buildconfig.yaml
│   ├── maintenance-mcp-buildconfig.yaml
│   ├── control-mcp-buildconfig.yaml
│   ├── waterplant-ui-buildconfig.yaml
│   └── kustomization.yaml
├── keycloak/
│   ├── operator/                  # RHBK operator (OperatorGroup + Subscription)
│   ├── keycloak/                  # PostgreSQL + Keycloak CR + Route + upstream realm
│   └── realms/
│       └── waterplant-realm-template.yaml   # Per-participant realm (operator persona)
├── mcp-gateway/                   # HTTPRoutes + MCPServerRegistrations for wp-dev servers
├── auth/                          # Auth policy reference docs (JWT validation)
├── authz/                         # Authz policy reference doc (tool-level CEL rules)
└── scripts/
    ├── add-participant.sh         # Provisions a new lab participant end-to-end
    └── provision-hermes-sandbox.sh  # Per-participant Hermes sandbox — called by
                                      # add-participant.sh, see hermes/README.md
```

Hermes itself (the OpenShell-sandboxed agent and its Helm chart) lives in
[`hermes/`](../hermes/) at the repo root, not under `deploy/` — see that
directory's own README for why. The retired chat-adapter-based design (an
alternative that kept network-policy enforcement at the cost of Hermes's own
streaming/session features) is preserved at `hermes-tmp/`.

---

## Known gaps

- **App Deployments/Services/Routes** — not captured in this repo.  They exist on the
  current cluster but must be recreated manually on a fresh cluster (e.g. via `oc new-app`
  or a GitOps manifest repo).
- **`x-mcp-servername` confirmation** — the Keycloak client IDs in the realm template use
  the convention `wp-dev/<registration-name>` (e.g. `wp-dev/telemetry-mcp`).  This must
  match exactly what the MCP Gateway broker injects as the `x-mcp-servername` header.  If
  tool calls return 403 despite correct role assignments, verify the actual value via
  Authorino logs:
  ```bash
  oc logs -n mcp-gateway -l app=authorino --tail=100 | grep x-mcp-servername
  ```
  and update the client IDs in `waterplant-realm-template.yaml` and any existing
  participant realms accordingly.
- **Hermes (Step 7) network-policy enforcement is a known, accepted gap in this
  build** — `hermes gateway run` runs in the sandbox pod's default network
  namespace (required for the Service to reach it at all), not through
  `openshell sandbox exec`'s policy-enforced one, so its own LLM/MCP/web calls
  are not currently subject to `openshellPolicy`'s network allowlist. See
  `hermes/README.md` and this repo's plan history for the full
  reachability-vs-enforcement tradeoff and the retired alternative
  (`hermes-tmp/`) that kept enforcement at the cost of Hermes's native
  streaming/session features.
- **Hermes is per-participant but the UI is single-tenant** — `waterplant-ui`
  has one `AGENT_URL`; only one participant's Hermes can be the live chat
  backend at a time. See Step 7d.
