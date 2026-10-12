"""Attacks and evaluations, run from the console against this participant's agent.

The lab ships seven attacks and six evals as YAML (`defs/`). This package runs
them: on a randomised timer, or on demand from the two buttons in the console
footer. It reaches the agent only through the same chat endpoint the console's
own chat pane uses, so there is no privileged side channel — anything a probe
achieves, the agent agreed to do when asked.

Why this lives in the console rather than in a shared cluster-wide runner (see
automation/gitops/bootstrap-infra/files/attack-eval-runner/, which specifies
one and implements none of it): the console already holds every credential and
endpoint a probe needs, it is already per-participant, and running here needs
no cross-namespace RBAC, no leader election and no second Deployment. The
widest privilege in the lab was the one thing the shared design could not avoid.
"""
