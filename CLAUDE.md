# RH1 AgentOps lab — working notes

Orientation for anyone (or any agent) picking this up. The authoritative documents
are alongside this file: [`implementation-plan.md`](implementation-plan.md) — read
**§4.0** first, it is the current status — plus `planning.md` and
`RH1 AgentOps Lab overview.md`. This file holds what those documents do not:
things learned the hard way that are expensive to re-derive.

## The two repositories

| Repo | Holds | Remote |
|---|---|---|
| **this one** | the agent and everything it talks to: `plant-api`, the three MCP servers, the console, our harness (`agent/`), Hermes (`hermes/`) | `rh-aiservices-bu/agentops-rh1-lab` |
| **workshop** | everything that *deploys* it: five Helm charts in `automation/gitops/`, and the Showroom lab guide | `rhpds/agentops-in-action-workshop` |

**Deployment manifests are not in this repo.** A change to how anything is deployed
is a change in the workshop repo, usually under `automation/gitops/tenant-platform`
(auto-sync), `tenant-policy` (manual sync — the participant's surface) or
`tenant-openshell`.

`agent/` is our own harness, and with `agent.harness: hermes` now the default it
serves as the BYOA control case rather than the shipped agent.

## Standing constraints

These are the project owner's rules, not suggestions.

- **Nothing runs locally, for anyone.** Everything is on OpenShift AI. No compose
  file, no local container builds. Images build on-cluster.
- **Pin every version. No automatic upgrades.** Subscriptions get
  `installPlanApproval: Manual` and an explicit `startingCSV`; vendored charts and
  fetched tarballs are pinned to a version or commit and checked against a digest.
- **Never expose secrets.** Read them with `read -rs`, write temp files under
  `umask 077`, pass them with `--from-file`, never echo a value, never put one in
  git or in chart values.

## Namespace shape, per participant

- `<user>-agentops` — plant-api, MCP servers, console, MCP Gateway, the agent.
  The participant has `edit` here.
- `<user>-openshell` — the OpenShell gateway, the sandbox Hermes runs in, and the
  bridge. The participant gets **read-only** access: sandbox pods run privileged,
  and the gateway's client certificate opens every sandbox behind it.

## Hard-won facts — do not re-derive these

### MLflow tracing

- Authorization is **Kubernetes RBAC**, not an MLflow concept. MLflow runs
  `--app-name=kubernetes-auth` with `self_subject_access_review`, so each request
  becomes a SelfSubjectAccessReview against group **`mlflow.kubeflow.org`** in the
  namespace named by the `X-MLflow-Workspace` header. Creating an experiment is
  `create` on `experiments`; posting spans is **`update`**. No CRD backs that group
  and none is needed — RBAC matches the strings.
- The authorizer **caches denials for 300s**. After granting RBAC, a retry inside
  that window still fails. Check RBAC with a SubjectAccessReview instead of
  inferring from the API's answer.
- **Two different path prefixes.** OTLP ingest is `POST /v1/traces` at the server
  root; the REST API is under `/mlflow/api/2.0/mlflow/...`. Fetching a trace's
  spans needs `/mlflow/ajax-api/3.0/mlflow/traces/get?trace_id=...` — the
  `api/3.0/mlflow/traces/{id}` route returns trace *info* only, with no spans.
- **Why the relay exists.** OpenShell enforces sandbox egress with a MITM proxy
  that re-originates TLS, and its trust store holds only public roots. MLflow's
  certificate is issued by `openshift-service-serving-signer`, so the proxy allows
  the connection and then drops the handshake — the OCSF log shows `NET:OPEN
  ALLOWED` then `NET:FAIL` ~20ms later. Nothing in the OpenShell chart or gateway
  configures that trust store. So nginx in the bridge relays plain HTTP to MLflow's
  HTTPS, verifying against the injected service CA, and **the sandbox policy names
  the relay, not MLflow**.
- The bridge ServiceAccount cannot mint its own token without an explicit Role
  (`serviceaccounts/token`, `resourceNames` scoped to itself). The projected token
  rotates; the sandbox holds a static file, so a long-lived token is minted.

### OpenShell

- The sandbox policy is an allowlist with **no "allow everything"** — `*`, `**` and
  TLD wildcards like `**.com` are refused. Every rule names hosts *and* the binaries
  permitted to use them, so a rule listing only `python3` will block `curl` to the
  same host.
- Filesystem rules are fixed at sandbox creation. A live sandbox accepts new paths
  but refuses to drop one; the bridge recreates the sandbox when a policy removes a
  path.
- The OCSF audit log inside the sandbox (`/var/log/openshell-ocsf.*.log`) records
  each decision with the policy that matched. It is the fastest way to tell "policy
  denied it" from "it broke after the policy allowed it".

### MCP Gateway and identity

- Tool authorization is two AuthPolicies: authentication on the public listener, and
  on the internal `mcps` listener a rule requiring `tool:<toolname>` under the client
  matching the MCP server. Verified: withheld role → 403, grant → 200, revoke → 403.
- One `operator` user per participant realm, with every tool declared as a role.
  `dump_plant_configuration` and `emergency_shutdown` are declared but **withheld** —
  those two withheld roles are Scenarios 3 and 4.
- Hermes lists tools **once at startup** and never refreshes, so the bridge gates
  startup on the gateway having listed every server's tools.

## Known open defect

The agent sometimes reports an action it never took — answers a control request in
prose, emits no tool call, and the plant is untouched. In a trace it looks like: an
`LLM` span, `finish_reason: stop`, **no `TOOL` span**, and no
`hermes.turn.tool_count` on the `agent` span. It did not reproduce in 12 controlled
replays of a known-failing conversation, so treat it as intermittent rather than
structural. Do not "fix" it by adding prompt language telling the model to be
honest — the prompt is not a policy surface here (see the README's subtlest-trap
section).

## Deliberate insecurity

Much of this code looks broken and is not. `plant-api` has no auth, `control-mcp`
exposes dangerous tools to everyone, `set_pump_speed` accepts any value, and tool
descriptions carry no prohibitive language. Those are the lab's starting conditions.
The README's "This code is deliberately insecure" table is the canonical list —
read it before repairing anything that looks like a defect.

## Shell gotchas (zsh on macOS)

- `$VAR:h` applies a **history modifier** (dirname), silently rewriting the value:
  `system:serviceaccount:$NS:name` becomes `system:serviceaccount:.ame`. Always
  brace: `${NS}:name`. This produced hours of false "permission denied" results.
- Quote anything with glob characters: `"...commits?per_page=1"`,
  `--include="*.yaml"` — unquoted they die with "no matches found".
- `oc exec -i` without `< /dev/null` swallows the rest of a heredoc script.
- Comments are not enabled interactively: an apostrophe in a pasted comment opens
  `quote>`.

## Conventions

- Commit messages: imperative summary, then *why* — what was wrong and what the
  change buys. The existing history is the model.
- Do not commit or push unless asked.
- Comments in code and charts explain **why**, including what was verified on which
  cluster. That convention is load-bearing here: several decisions look arbitrary
  without the finding that produced them.
