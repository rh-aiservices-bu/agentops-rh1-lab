# hermes/ — Hermes agent, OpenShell-sandboxed

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

## Why there's a Containerfile here but no build in CI

`hermes/Containerfile` is kept for provenance/reproducibility, not built by
this repo's CI. Like every other component, the image is built externally and
pushed to `quay.io/rh-aiservices-bu` — the workshop repo's `tenant-platform`
chart pins the tag actually deployed (`agent.hermes.image.*`). Rebuild only if
you need to bump a baked-in dependency, or when you change `hermes-agent`'s
own source (the fork carries patches to its `_check_auth` and `mcp_tool.py` —
see the workshop repo for what they do and why).

```bash
podman build --platform linux/amd64 -t hermes-openshell:latest -f hermes/Containerfile hermes/
podman push hermes-openshell:latest quay.io/rh-aiservices-bu/waterplant-hermes:<tag>
```

## Where the rest of this lives

Everything about how Hermes is actually deployed and configured — the
OpenShell sandbox provisioning, the gateway relay architecture, MCP identity
and token refresh, MLflow tracing wiring, Keycloak/authz, and the known
functional gaps and bugs that shaped that design — lives in the workshop
repo's (`agentops-in-action-workshop`) `automation/gitops/tenant-platform` and
`tenant-openshell` charts, not here. This repo only builds the image.
