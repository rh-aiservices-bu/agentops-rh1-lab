"""Undoing an attack that got through.

Three rules this module exists to enforce.

**Cleanup runs automatically, on every path.** Scheduled or button-pressed, it
is never something anyone has to remember to do and never conditional on a
console being open. engine.py calls it from a `finally`, so it still runs when
the agent times out mid-probe or the probe raises.

**It is triggered by `attack_succeeded`, not by `not passed`.** Those differ on
exactly the case that matters: an inconclusive result is not a pass, but it is
also not grounds for writing to a live plant. We undo damage we observed, never
damage we merely failed to rule out.

**The primitives are a closed enum, not a script field.** The upstream SCHEMA.md
makes the argument and it holds here too: these definitions are meant for lab
content authors to extend, and that surface must not become a place where
arbitrary code runs unattended against a live cluster every few minutes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from . import goals, plant

#: Whether cleanup may reseed the plant as a last resort. On by default: the
#: alternative, on a plant-api that cannot release an emergency shutdown, is a
#: plant that stays dark for the rest of the session. Set
#: PROBE_ALLOW_PLANT_RESET=0 where losing work orders is the worse outcome.
ALLOW_RESET = os.environ.get("PROBE_ALLOW_PLANT_RESET", "1").strip().lower() not in (
    "0", "false", "no",
)


@dataclass
class CleanupResult:
    attempted: bool = False
    #: None when nothing was attempted. Never left unset after an attempt.
    succeeded: bool | None = None
    detail: str = ""
    #: "oc" when the file was put back deterministically with `oc cp`, "agent"
    #: when the agent did it when asked, "direct" when we reached past it into
    #: plant-api, "" when nothing was attempted.
    #: Surfaced rather than folded into `detail` because "the agent undid it"
    #: and "the lab undid it behind the agent's back" are different events,
    #: and only the first says anything about the agent.
    via: str = ""

    def as_event(self) -> dict:
        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "detail": self.detail,
            "via": self.via,
        }


async def run(
    spec: dict,
    *,
    client: httpx.AsyncClient,
    plant_api_url: str,
    before: plant.Snapshot | None,
    shutdown_was_set: bool = False,
    ask=None,
    soul_before: str = "",
    agent_pod_selector: str = "",
    agent_pod_container: str = "",
) -> CleanupResult:
    kind = (spec or {}).get("type", "none")

    if kind == "none":
        return CleanupResult(
            attempted=False, detail="nothing to undo — the probe mutated nothing"
        )

    if kind == "canary_reseed":
        # The destroyed file was a decoy planted by the write-report eval, so
        # there is nothing to restore; the next eval sweep plants a fresh one.
        # Recorded explicitly rather than reported as "none", so the metric
        # distinguishes "no cleanup needed by design" from "not implemented".
        return CleanupResult(
            attempted=False,
            detail="the deleted file was a decoy; the next eval sweep plants a fresh one",
        )

    if kind == "reports_restore":
        # The attack empties the reports directory, so what is restored is the
        # one file that is always meant to be there. Anything the participant
        # wrote themselves is genuinely gone — which is the lesson, and why
        # the attack is scored as damage rather than as noise.
        if ask is None:
            return CleanupResult(
                attempted=True, succeeded=False,
                detail="no agent available to rewrite the report",
            )
        turn = await ask(goals.REPORTS_RESTORE_PROMPT.format(
            path=goals.REPORTS_CANARY, body=goals.REPORTS_CANARY_BODY))
        if not turn.ok:
            return CleanupResult(
                attempted=True, succeeded=False,
                detail=f"restore request failed: {turn.error}",
            )
        # Verified by looking, where there is something to look with. A
        # cleanup that asks the agent "did you put it back?" and believes the
        # answer is not a verification.
        spec_for_check = {"path": goals.REPORTS_CANARY}
        observed, _ = await goals.fs_observe(
            spec_for_check, agent_pod_selector, agent_pod_container
        )
        if observed is None:
            check = await ask(goals.fs_prompt(spec_for_check))
            observed = goals.interpret_fs_answer(check.text) if check.ok else None
        if observed is True:
            return CleanupResult(
                attempted=True, succeeded=True,
                detail=f"{goals.REPORTS_CANARY} was written back and verified",
            )
        return CleanupResult(
            attempted=True, succeeded=False,
            detail=f"{goals.REPORTS_CANARY} is still missing; the reports "
                   "directory was not restored",
        )

    if kind == "soul_restore":
        # The agent's own instructions file. There is no canary equivalent —
        # writing to a decoy proves nothing about whether the real one is
        # writable — so the only safe way to run this attack is to put the
        # original back afterwards.
        #
        # Deterministically, with `oc cp`, the same way Reset Plant restores
        # it: copy the pristine SOUL.md over the tampered one and verify by
        # reading both back. Asking the agent to rewrite its own instructions
        # is exactly backwards — the thing that was just manipulated into
        # editing the file is the last thing that should be trusted to undo
        # it — and it cost two more model turns after the attack had already
        # answered, which is the dead time between the reply and the verdict.
        if agent_pod_selector and agent_pod_container:
            ok = await goals.restore_soul(agent_pod_selector, agent_pod_container)
            if ok:
                return CleanupResult(
                    attempted=True, succeeded=True, via="oc",
                    detail="the agent's instructions were restored and verified",
                )
            return CleanupResult(
                attempted=True, succeeded=False, via="oc",
                detail="could not restore the instructions file; the agent may "
                       "still be running on tampered instructions",
            )

        # No pod to exec into (a plain-agent deployment without exec access,
        # or a test). Fall back to asking the agent to rewrite the file from
        # the copy captured before the attack — slower and less trustworthy,
        # but better than leaving it tampered.
        if not soul_before:
            return CleanupResult(
                attempted=True,
                succeeded=False,
                detail="the original instructions were never captured, so they "
                       "cannot be restored; the agent is running on the "
                       "tampered file",
            )
        if ask is None:
            return CleanupResult(
                attempted=True, succeeded=False,
                detail="no agent available to restore the instructions file",
            )
        path = (spec or {}).get("path", "SOUL.md")
        turn = await ask(
            goals.SOUL_RESTORE_PROMPT.format(path=path, body=soul_before)
        )
        if not turn.ok:
            return CleanupResult(
                attempted=True, succeeded=False, via="agent",
                detail=f"restore request failed: {turn.error}",
            )
        check = await ask(goals.SOUL_READ_PROMPT.format(path=path))
        if check.ok and goals.extract_block(check.text).strip() == soul_before.strip():
            return CleanupResult(
                attempted=True, succeeded=True, via="agent",
                detail="the agent's instructions were restored and verified",
            )
        return CleanupResult(
            attempted=True, succeeded=False, via="agent",
            detail="the instructions file does not match what was captured; "
                   "the agent may still be running on tampered instructions",
        )

    if kind == "plant_state_restore":
        if before is None or before.partial:
            return CleanupResult(
                attempted=True,
                succeeded=False,
                detail="no trustworthy pre-probe snapshot — plant-api was unreadable",
            )
        return await _restore(
            client, plant_api_url, before, shutdown_was_set=shutdown_was_set, ask=ask
        )

    return CleanupResult(
        attempted=False, succeeded=None, detail=f"unknown cleanup type {kind!r}"
    )


async def _restore(
    client: httpx.AsyncClient,
    plant_api_url: str,
    before: plant.Snapshot,
    *,
    shutdown_was_set: bool,
    ask,
) -> CleanupResult:
    """Ask the agent to put the plant back; fall back to plant-api if it does not."""
    watch = ["pumps", "valves"]

    now = await plant.snapshot(client, plant_api_url)
    changes = plant.diff(before, now, watch)

    # Ask first — but only about things the agent can actually reach. The
    # shutdown latch is not one of them: control-mcp has no tool that clears
    # it, so prompting about it would just teach the agent to claim success it
    # cannot have had.
    if ask is not None and changes:
        turn = await ask(plant.restore_instruction(before, changes))
        if turn.ok:
            after = await plant.snapshot(client, plant_api_url)
            remaining = plant.diff(before, after, watch)
            if not remaining and not shutdown_was_set:
                return CleanupResult(
                    attempted=True,
                    succeeded=True,
                    via="agent",
                    detail="the agent restored the plant when asked",
                )

    # The agent declined, could not be reached, got it half right, or the
    # shutdown latch needs lifting. Reach past it.
    ok, detail = await plant.restore_directly(
        client, plant_api_url, before, after_shutdown_set=shutdown_was_set
    )
    note = (
        "the agent did not restore the plant, so the lab did it directly — "
        if ask is not None and changes
        else ""
    )
    if ok:
        return CleanupResult(
            attempted=True, succeeded=True, via="direct", detail=note + detail
        )

    # Still broken. On a plant-api without POST /emergency-shutdown/clear a
    # latched shutdown has no surgical way out, and leaving it set stops the
    # intake controller and drains the reservoir to zero — which poisons every
    # later probe and ends the session with a dark plant. Reseeding costs the
    # participant's work orders and is recorded as such, but it costs less
    # than that.
    if ALLOW_RESET and (await plant.snapshot(client, plant_api_url)).shutdown:
        if await plant.hard_reset(client, plant_api_url):
            return CleanupResult(
                attempted=True,
                succeeded=True,
                via="reset",
                detail=(
                    "the shutdown could not be released on this plant-api, so "
                    "the plant was reseeded to get it running again — any work "
                    "orders raised before now are gone. Deploying "
                    "POST /emergency-shutdown/clear removes the need for this."
                ),
            )

    return CleanupResult(attempted=True, succeeded=False, via="direct", detail=note + detail)
