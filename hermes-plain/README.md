# hermes-plain/ — Hermes agent, no sandbox

The same upstream agent as [`hermes-openshell/`](../hermes-openshell/)
([RHRolun/hermes-agent](https://github.com/RHRolun/hermes-agent), a fork of
NousResearch's Hermes), but built as a plain headless container — no
OpenShell sandbox, no Landlock policy, no sandbox-specific startup script.
Just the gateway process (`hermes gateway run`) behind a health check on
`:8787`, running non-root under `tini`.

This is not currently deployed anywhere in this lab — it was imported from
[`cai-krew/hermes-container`](https://github.com/rh-aiservices-bu/cai-krew)
(its plain `Containerfile`, the ancestor of `hermes-openshell/Containerfile`'s
`Containerfile.openshell` variant) for future use, e.g. if a scenario ever
needs Hermes without sandbox governance for comparison.

## Why there's a Containerfile here but no build in CI

Same reason as `hermes-openshell/`: kept for provenance/reproducibility, not
built by this repo's CI. Rebuild only if you need to bump a dependency or pick
up a `hermes-agent` upstream change.

```bash
podman build --platform linux/amd64 -t hermes-plain:latest -f hermes-plain/Containerfile hermes-plain/
podman push hermes-plain:latest quay.io/rh-aiservices-bu/waterplant-hermes-plain:<tag>
```

## What was deliberately not imported

The source directory also had `entrypoint.sh` and `env.example` — a
docker-compose-style local run path (env-var injection into `config.yaml`/
`.env`, `SIGTERM` trap, etc.). Not brought over: this repo's standing rule is
that nothing runs locally, for anyone, and there is no compose file here —
every image builds and pushes to quay, then runs only on OpenShift AI. If a
local inner-dev-loop for Hermes is ever needed, that's a deliberate decision
to revisit, not something to reintroduce quietly by copying these files back.

It also had a Helm chart (`chart/`) deploying this image (plus two dashboard
variants, `Containerfile.ui`/`Containerfile.webui`, neither imported here) as
a plain Kubernetes Deployment+Service+Route. Deployment manifests don't live
in this repo — see `CLAUDE.md`. If this variant is ever actually deployed,
that content belongs in the workshop repo (`agentops-in-action-workshop`),
not here.
