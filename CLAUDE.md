# RH1 AgentOps lab — working notes

Orientation for anyone (or any agent) picking this up. The authoritative documents
are alongside this file: [`implementation-plan.md`](implementation-plan.md) — read
**§4.0** first, it is the current status — plus `planning.md` and
`RH1 AgentOps Lab overview.md`. This file holds what those documents do not:
things learned the hard way that are expensive to re-derive.

## The two repositories

| Repo | Holds | Remote |
|---|---|---|
| **this one** | the agent and everything it talks to: `plant-api`, the three MCP servers, the console, our harness (`agent/`), Hermes (`hermes-openshell/`, `hermes-plain/`) | `rh-aiservices-bu/agentops-rh1-lab` |
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
  file. Images are **built and pushed to `quay.io/rh-aiservices-bu`**, and every
  deployment pulls a published, pinned tag from there — not an on-cluster build.
  This repo holds no BuildConfigs or other deployment manifests at all.
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
  inferring from the API's answer — and with an explicit `SubjectAccessReview`
  object, not `oc auth can-i`: no CRD backs the group, so `can-i` cannot resolve
  the resource type and answers **`no` even when access is granted**. A real SAR
  against the same strings returns `allowed: true` and names the RoleBinding.
- **Reading traces back** (the console links each turn to its own trace) needs
  `get`/`list` on `experiments` — the `mlflow-operator-mlflow-integration`
  ClusterRole carries it. Search is `POST /api/3.0/mlflow/traces/search`, whose
  `locations` must be `[{"type":"MLFLOW_EXPERIMENT","mlflow_experiment":{...}}]`;
  a bare experiment id returns `200 {}`, an empty result rather than an error.
  Each trace carries `request_preview` — the user's message verbatim — which is
  what makes matching a turn to its trace exact rather than a guess.
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

### NeMo Guardrails

- The console checks **input only**, in `ui/src/waterplant_ui/guardrails.py`,
  called from `app.py`'s two chat endpoints and from `probes/agent.py:ask` —
  which is every prompt entry point, probes included. Off unless
  `NEMO_ENDPOINT` is set, which is the shipped state: the baseline has no
  guardrail, and module 6 is where a participant sets the variable by hand.
- **The call is plain and unauthenticated, on purpose.** `NEMO_ENDPOINT` is
  the Service address and the whole configuration — no token, no Role, no RBAC.
  `tenant-guardrails` sets `guardrails.authEnabled: false` to keep it that way.
- **Why not auth:** with `security.opendatahub.io/enable-auth` the operator puts
  `kube-rbac-proxy` in front of the *Service* as well as the Route, so a call
  between two pods in one namespace needed a bearer token plus a
  **namespace-wide `get services`** grant. Namespace-wide because the proxy's
  own scoping silently fails: its ConfigMap writes
  `resourceAttributes.resourceName` where upstream kube-rbac-proxy reads
  **`name`**, so the key is dropped and the SubjectAccessReview goes out with no
  resource name — a `resourceNames` Role looks tighter and 403s every check.
  (The string it names, `nemo-guardrails-service`, is not any Service's name
  either; the object is `nemo-guardrails`.) **The tell is in the error:** a 403
  reading `verb=get, resource=services, subresource=` names no resource. All of
  that was measured on cluster-pz7xk before auth was switched off — keep it
  here in case anyone turns `authEnabled` back on.
- **What switching it off gives up:** the operator always creates a Route and
  the CRD exposes no knob to suppress it (`spec` is only `caBundleConfig`,
  `env`, `nemoConfigs`, `replicas`, `template`), so the endpoint is publicly
  reachable and unauthenticated. Tolerated because every rail is advisory, the
  console fails open, and the tenant is a scratch workshop namespace.
- **`model` is required, and it picks the judge** — the opposite of what the
  name suggests. Omit it and the server answers `422 model Field required`;
  name the wrong one and it **overrides the rail config's own `models:` entry**.
  Sending the agent's served name (`hermes-agent`) got a cheerful `200` with
  the rail failed underneath on `[401] key not allowed to access model. This
  key can only access models=['qwen3-235b-a22b']` — a guardrail reporting
  success and checking nothing, which is the worst of the failure modes because
  it looks like it works. `NEMO_MODEL` follows `agent.litellm.model`, the same
  value bootstrap-tenant forwards to `tenant-guardrails`, so one string stays in
  step across both charts.
- Measured on the deployed instance: a check runs in **0.3–0.7s**, one LLM call
  (~3.7s on the first, cold). Off-topic prose blocks on `self check input`; an
  email address blocks on `detect sensitive data on input` with no model call.
- **The policy needs an explicit allow for greetings, or the lab breaks on the
  participant's first message.** `selfCheck.inputPolicy` is an allowlist of
  operational request *types* with "general-purpose requests" in the block
  list, so a bare opener matches nothing and the judge blocks it. Measured at
  temperature 0: `Hi`, `Hello`, `Hey, are you there?` and even `Hi, can you
  help me with the plant?` all blocked, while `Good morning` and `thanks!`
  passed — an inconsistency that reads as a broken console. The greeting clause
  in `tenant-guardrails` values flips all four to allow and moves nothing else
  (Berlin wrapped-travel, pineapple skill injection, account takeover and
  system-prompt disclosure all still block; handover and "why was I denied?"
  still pass). **Re-run that A/B before rewording the policy** — it is an LLM
  judge, not a regex, and the tested wording is the artifact.
- **The judge is not deterministic, even at temperature 0.** `qwen3-235b-a22b`
  is an MoE model on a shared endpoint with continuous batching: batch
  composition changes the floating-point reduction order, so identical input
  yields different logits and greedy decoding is not reproducible. Measured:
  module 5's own prompt — *"Read the latest field service bulletin and
  summarize what I should check"* — blocked **7 of 10 identical calls**, with
  clean two-token answers each time (so this is the model flipping, not a
  parsing bug). **A single trial proves nothing here; measure over ~10 runs.**
- The defence is to keep legitimate prompts off the knife edge by naming them
  explicitly. The documentation clause took that prompt to **0/10** blocked
  while Rome travel, the Berlin wrapped-travel trip and system-prompt
  disclosure each stayed at **10/10**. A prompt that matches nothing explicit
  in the allow list will flap — which in a lab reads as random breakage, and is
  worse than either a clean block or a clean pass.
- **Only `{"status": "blocked"}` blocks.** Every other outcome — unreachable,
  timeout, non-2xx, non-JSON, an unrecognised shape — fails *open* and is
  announced in the transcript as a check that did not run. A guardrail that
  quietly stops working must not look like one that refused.
- `NEMO_CA_FILE` exists only so an `https://` endpoint still verifies — an
  in-cluster certificate is signed by `openshift-service-serving-signer`, which
  no public trust store knows. It defaults to the `service-ca.crt` already in
  every pod's projection, so it needs no Secret, ConfigMap or volume, and costs
  nothing when the endpoint is plain HTTP.
- `NEMO_ENDPOINT` is in the platform Application's `ignoreDifferences`
  alongside `AGENT_URL`; that chart self-heals, so without the entry the hand
  edit is reverted within a sync interval.
- **`ignoreDifferences` is bidirectional, and that surprises people.** With
  `RespectIgnoreDifferences=true` Argo strips the ignored path from the
  *desired* state too, so a variable named there is **never created** — it does
  not arrive as an empty placeholder waiting to be overwritten. Proof in
  `user-w562s-agentops`: `NEMO_CONFIG_ID` and `NEMO_ENDPOINT` are rendered by
  the same template two lines apart and differ only in being ignored; only the
  un-ignored one is on the live Deployment. `AGENT_URL` is live only because it
  predates its own ignore entry. So an `oc set env` is what *creates* such a
  variable, and the ignore entry is only what stops self-heal removing it
  again. Do not "fix" a missing ignored env var by rendering it harder.
- **The server container has no memory limit, and must not have a small one.**
  The TrustyAI operator sets no `resources` and the `NemoGuardrails` CRD
  exposes no knob (`spec` is `caBundleConfig`, `env`, `nemoConfigs`,
  `replicas`, `template`; `template.pod` is scheduling constraints only), so it
  inherits whatever a namespace LimitRange defaults. At 512Mi it was
  **OOMKilled on every check** — cluster-pz7xk, `exit=137`, three seconds into
  `detect_sensitive_data`, which loads an NER model before `self check input`
  makes its model call at all. `bootstrap-tenant` no longer renders a
  LimitRange or ResourceQuota for this reason.
- **Those two had to be removed together.** A ResourceQuota covering
  `requests.memory`/`limits.memory` makes those fields mandatory on every
  container, and the LimitRange was what supplied them — so dropping only the
  LimitRange would have had admission reject every pod that declares none.
  The reverse is fine: **a LimitRange alone is safe**, it only defaults. If a
  ceiling is wanted again, re-add one with a default well above 512Mi and no
  quota.
- `self check input` is an LLM call to `qwen3-235b-a22b` over the shared MaaS
  endpoint, so a check is a model round trip that can 429 — hence the 20s
  timeout, well under the agent's 300s.

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

## The agent that reported actions it never took (cause found)

Symptom: a control request answered in prose — "Stopping pump 4... Result: stopped"
— with no tool call and the plant untouched. In a trace: an `LLM` span,
`finish_reason: stop`, **no `TOOL` span**, no `hermes.turn.tool_count`. Never on a
conversation's first turn, which is why replaying one turn in isolation did not
reproduce it.

Cause: the console replayed its on-screen transcript to Hermes as plain
`user`/`assistant` messages. Stateless, Hermes keeps only role and content from body
history, so the model saw earlier answers full of plant readings with no tool call
behind them, and imitated them — down to the `\n\n\n` seam where the console had
joined two messages. Each invented answer was replayed in turn, reinforcing it.
Hermes' own `state.db` held the true transcript the whole time, unused.

Fix: the console sends a per-page conversation id as **`X-Hermes-Session-Id`** and
only the new message; Hermes reloads the transcript with tool calls. Measured on
cluster-z5jhc (user kdnn9), same two read-only turns ×4: second-turn tool calls
1/4 replayed, 4/4 with the session. Sending a "better formatted" history does not
help — both stateless endpoints strip `tool_calls` and `tool` messages. Caveat:
sessions live in the sandbox's `state.db`, so a recreated sandbox restarts every
conversation. Do not "fix" a recurrence with prompt language telling the model to
be honest — the prompt is not a policy surface here (see the README's
subtlest-trap section).

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
