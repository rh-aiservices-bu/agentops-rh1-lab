# hermes-openshell/ — Hermes agent, OpenShell-sandboxed

A second AI agent for this lab, alongside `agent/`. Where `agent/` is a
purpose-built agent with an explicit tool-authorization contract (D8:
propagates the caller's token, makes zero authz decisions itself), Hermes is
an existing general-purpose CLI agent ([RHRolun/hermes-agent](https://github.com/RHRolun/hermes-agent),
a fork of NousResearch's Hermes) run inside an
[OpenShell](https://github.com/nvidia/openshell) sandbox — OpenShell enforces
a Landlock filesystem/network policy around whatever Hermes tries to do,
which is the point: it's the "how do you govern an agent you don't control
the source of" half of the lab, as opposed to `agent/`'s "how do you build
one safely from scratch" half.

This is the **sandboxed ("boxed")** variant. [`hermes-plain/`](../hermes-plain/)
is the same upstream agent built as a plain headless container with no
OpenShell sandbox around it — see that directory's README for why both exist
and how they differ.

## Why there's a Containerfile here but no build in CI

`hermes-openshell/Containerfile` is kept for provenance/reproducibility, not built
by this repo's CI. Like every other component, the image is built externally
and pushed to `quay.io/rh-aiservices-bu` — the workshop repo's `tenant-platform`
chart pins the tag actually deployed (`agent.hermes.image.*`). Rebuild only if
you need to bump a baked-in dependency, or when you change `hermes-agent`'s
own source (the fork carries patches to its `_check_auth` and `mcp_tool.py` —
see the workshop repo for what they do and why).

```bash
podman build --platform linux/amd64 -t hermes-openshell:latest -f hermes-openshell/Containerfile hermes-openshell/
podman push hermes-openshell:latest quay.io/rh-aiservices-bu/waterplant-hermes:<tag>
```

## Containerfile.cli — the participant's OpenShell CLI

`Containerfile.cli` builds a small UBI image with the `openshell` CLI and an
ssh client. It runs as the `cli` container in the workshop's
`hermes-openshell-bridge` pod, where participants create the sandbox and start
Hermes themselves; the bridge also copies `openshell` out of it for its own
supervisor. The ssh client is what `openshell sandbox create`'s initial command
and `openshell sandbox upload` need. Same build model as above; the deployed
tag is `bridge.openshellCliImage` in the workshop repo's `tenant-openshell`
chart.

```bash
podman build --platform linux/amd64 -t openshell-cli:0.1 -f hermes-openshell/Containerfile.cli hermes-openshell/
podman push openshell-cli:0.1 quay.io/rh-aiservices-bu/openshell-cli:0.1
```

## Where the rest of this lives

Everything about how Hermes is actually deployed and configured — the
OpenShell sandbox provisioning, the gateway relay architecture, MCP identity
and token refresh, MLflow tracing wiring, Keycloak/authz, and the known
functional gaps and bugs that shaped that design — lives in the workshop
repo's (`agentops-in-action-workshop`) `automation/gitops/tenant-platform` and
`tenant-openshell` charts, not here. This repo only builds the image.
