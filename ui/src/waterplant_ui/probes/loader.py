"""Loading and rendering the attack/eval definitions.

The definitions are lab content, not code. They live in the workshop repo at
automation/gitops/tenant-platform/files/attack-eval-defs/ and are mounted into
this container as a ConfigMap at PROBE_DEFS_DIR. This image ships none of its
own: it is a runtime for them.

That means an unmounted console has no attacks and no evals, which is a state
it has to report rather than render as "nothing is armed" — see
`load_problem`. The alternative, keeping a copy in the image as a fallback,
puts the same YAML in two repos and invites exactly the drift this replaced:
the two sets had already diverged into `assertion:` and `goal:` blocks, and a
prompt fix had to be applied twice.

Those `assertion:` blocks asserted on OpenTelemetry span attributes that no
deployed harness emits, so none of them could be evaluated. `goal` blocks
replaced them; see goals.py.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

#: Where the ConfigMap is mounted. The chart and the dev manifest both set
#: this; the default matches them so a hand-run container behaves the same.
DEFS_DIR = os.environ.get("PROBE_DEFS_DIR", "/config/probe-defs").strip()

#: Makes canary filenames unique per run, so a stale canary from an earlier
#: run can never satisfy or defeat a later one.
_CYCLE_VAR = re.compile(r"\{\{\s*cycle_id\s*\}\}")

#: The documentation site's base URL. Templated rather than written out
#: because it names a Service in the participant's own namespace, which a
#: definition in git cannot know. Unset, it renders empty — visible in the
#: prompt and in the transcript, rather than silently pointing at nothing.
_DOCS_VAR = re.compile(r"\{\{\s*docs_url\s*\}\}")


@dataclass(frozen=True)
class Case:
    """One attack or eval, as loaded from disk."""

    id: str
    kind: str  # "attack" | "eval"
    title: str
    description: str
    blurb: str
    severity: str
    enabled: bool
    disabled_reason: str
    request: dict[str, Any]
    goal: dict[str, Any]
    polarity: str
    weight: int
    metric_name: str
    cleanup: dict[str, Any]
    plants_canary: str
    requires_canary: str
    tags: list[str] = field(default_factory=list)
    maps_to_module: str = ""

    @property
    def prompts(self) -> list[str]:
        """The turns to send, whether the case declares one or several."""
        if self.request.get("type") == "multi_turn":
            return list(self.request.get("turns", []))
        return [self.request.get("prompt", "")]


def render(value: Any, cycle_id: str, docs_url: str = "") -> Any:
    """Substitute {{ cycle_id }} and {{ docs_url }} through a nested structure."""
    if isinstance(value, str):
        return _DOCS_VAR.sub(docs_url.rstrip("/"), _CYCLE_VAR.sub(cycle_id, value))
    if isinstance(value, dict):
        return {k: render(v, cycle_id, docs_url) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, cycle_id, docs_url) for v in value]
    return value


def _case(raw: dict[str, Any]) -> Case:
    scoring = raw.get("scoring") or {}
    return Case(
        id=raw["id"],
        kind=raw["kind"],
        title=raw["title"],
        description=" ".join((raw.get("description") or "").split()),
        # Falls back to the title so a def that forgets a blurb still produces
        # a readable toast rather than an empty one.
        blurb=raw.get("blurb") or raw["title"],
        severity=raw.get("severity", "medium"),
        enabled=bool(raw.get("enabled", True)),
        disabled_reason=" ".join((raw.get("disabled_reason") or "").split()),
        request=raw.get("request") or {},
        goal=raw.get("goal") or {},
        polarity=raw["polarity"],
        weight=int(scoring.get("weight", 1)),
        metric_name=scoring.get("metric_name", f"{raw['kind']}.{raw['id']}"),
        cleanup=raw.get("cleanup") or {"type": "none"},
        plants_canary=raw.get("plants_canary", ""),
        requires_canary=raw.get("requires_canary", ""),
        tags=list(raw.get("tags") or []),
        maps_to_module=raw.get("maps_to_module", ""),
    )


def _yaml_files(root: Path) -> list[Path]:
    """Every definition under `root`, each exactly once.

    A Kubernetes ConfigMap mount is not a plain directory: the real files sit
    in a hidden, timestamped `..2026_10_12_...` directory with a `..data`
    symlink and a symlink per key beside it. A plain rglob walks both and
    returns every definition twice — which is how a 7-attack suite started
    reporting 14, each one running and being scored twice per sweep.

    Skipping dot-dot components covers that without special-casing the mount,
    and leaves the bundled attacks/ + evals/ layout working.
    """
    return sorted(
        p for p in root.rglob("*.yaml")
        if not any(part.startswith("..") for part in p.relative_to(root).parts)
    )


def defs_dir() -> Path:
    return Path(DEFS_DIR)


def load_problem(defs_dir_override: Path | None = None) -> str:
    """Why there are no definitions, or "" when there are.

    Worth its own function because the two failures look identical on screen
    and have nothing to do with each other: a console whose attacks are all
    disabled and a console that was never given any definitions both render as
    an empty catalogue. The second is a deployment fault and has to say so.
    """
    root = defs_dir_override or defs_dir()
    if not root.exists():
        return (
            f"no definitions are mounted at {root} — the console is a runtime "
            "for them and ships none of its own. Check the "
            "water-plant-ui-probe-defs ConfigMap and its volumeMount."
        )
    if not _yaml_files(root):
        return (
            f"{root} is mounted but holds no YAML. A ConfigMap that rendered "
            "empty does this: Helm's .Files.Glob is filepath.Match, so a "
            "pattern with braces or ** matches nothing and fails silently."
        )
    return ""


def load_all(defs_dir_override: Path | None = None) -> list[Case]:
    """Every case, attacks and evals, enabled or not.

    Sorted by id so the catalogue the console renders has a stable order
    between restarts — an attack list that reshuffles itself on every pod
    rollout is needlessly confusing to read.
    """
    root = defs_dir_override or defs_dir()
    cases = []
    for path in _yaml_files(root):
        try:
            cases.append(_case(yaml.safe_load(path.read_text(encoding="utf-8"))))
        except (yaml.YAMLError, KeyError, TypeError) as exc:
            # One malformed file must not take out the whole suite. A lab
            # author editing a prompt in the gitops repo will occasionally
            # push broken YAML, and losing every other case over it — with no
            # clue which file did it — is a bad way to find out.
            log.error("skipping unreadable definition %s: %s", path.name, exc)
    return sorted(cases, key=lambda c: (c.kind, c.id))


def attacks(cases: list[Case], *, enabled_only: bool = True) -> list[Case]:
    return [c for c in cases if c.kind == "attack" and (c.enabled or not enabled_only)]


def evals(cases: list[Case], *, enabled_only: bool = True) -> list[Case]:
    return [c for c in cases if c.kind == "eval" and (c.enabled or not enabled_only)]
