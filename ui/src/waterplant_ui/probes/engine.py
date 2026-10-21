"""Running one case, judging it, and putting the plant back.

The shape of a probe:

    snapshot  ->  ask the agent  ->  observe the goal  ->  cleanup  ->  emit

Two events come out of every run, `started` then `resolved`, so the console can
raise a toast the instant an attack begins and resolve it in place rather than
leaving a dead minute where something is happening and nothing says so.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import agent, cleanup, events, goals, loader, mlflow_run, plant
from .loader import Case

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    case_id: str
    title: str
    #: "blocked" | "declined" | "got_through" | "inconclusive" for attacks;
    #: "passed" | "failed" | "inconclusive" for evals.
    #:
    #: "declined" is a real third state, not a shade of "blocked": the attack
    #: did not land, but nothing stopped it — the agent chose not to comply.
    #: That is not a defence, because the next model, the next phrasing or the
    #: next day may decide differently, and the participant's policy was never
    #: exercised. Telling them "blocked" there is the console lying about the
    #: one thing it exists to report.
    outcome: str = "inconclusive"
    detail: str = ""
    cleanup: dict[str, Any] = field(default_factory=dict)
    #: The conversation this case ran under, so its MLflow trace can be found
    #: by exact match rather than by recency.
    session_id: str = ""

    @property
    def passed(self) -> bool:
        """Did the bad thing fail to happen? `declined` counts; it is just weak."""
        return self.outcome in ("blocked", "declined", "passed")

    @property
    def conclusive(self) -> bool:
        return self.outcome != "inconclusive"


#: Reseed the plant before every attack, so each one is judged from an
#: identical baseline.
#:
#: Off by default, and that is not timidity. Every reset discards the
#: participant's work orders and maintenance records, so with the scheduler
#: running this would quietly destroy their work every five to ten minutes.
#: Correctness does not need it either: each attack snapshots the plant
#: immediately beforehand and cleanup restores to *that*, so the measurement
#: is already a clean delta from whatever was there. What a reset buys is a
#: byte-identical starting point, which matters for a reproducible demo and
#: not much else. Turn it on for one.
RESET_BEFORE_ATTACK = os.environ.get(
    "PROBE_RESET_BEFORE_ATTACK", "0"
).strip().lower() in ("1", "true", "yes")
#: Reseed before an eval sweep. Off by default here; the chart turns it on.
#:
#: It was off for a while, on the argument that a reset discards every work
#: order and maintenance record the participant has raised, and doing that
#: silently as a side effect of pressing "run evals" destroys work they did
#: not offer up. The counter-argument won: two scores only compare if both
#: started from the same plant, and comparing one sweep against another —
#: before a control and after it — is what the lab asks participants to do.
#:
#: What makes it defensible is that it is announced rather than silent.
#: Module 1 tells them the suite resets the plant and clears the transcript,
#: and to finish inspecting their manual requests first. Nothing happens
#: unless they press the button.
#:
#: Still off in this file's default, so a console deployed without the chart
#: behaves conservatively. tenant-platform sets ui.probes.resetBeforeEvals.
RESET_BEFORE_EVALS = os.environ.get(
    "PROBE_RESET_BEFORE_EVALS", "0"
).strip().lower() in ("1", "true", "yes")


def new_cycle_id() -> str:
    """Short, unique, filename-safe. Keeps each run's canary distinct."""
    return secrets.token_hex(4)


class Engine:
    """Runs probes against one participant's agent.

    One instance per process. `_lock` serialises everything: two probes at once
    would race each other's plant snapshots and restore each other's damage,
    and the whole point of the snapshot is that nothing else moved in between.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        agent_url: str,
        plant_api_url: str,
        docs_url: str = "",
        agent_pod_selector: str = "",
        agent_pod_container: str = "",
        cases: list[Case] | None = None,
        timeout_s: float = 180.0,
    ) -> None:
        self._client = client
        self._agent_url = agent_url
        self._plant_api_url = plant_api_url
        #: The documentation site. Two jobs: it serves the pages the doc
        #: cases ask the agent to read, and it records what reached its
        #: attacker path — so a honeypot goal is judged by what arrived
        #: rather than by what the agent said it did. Empty means no
        #: observer, and those goals stay unjudgeable rather than passing.
        #:
        #: One URL for both the engine and the agent, because both reach it
        #: by Service name. That takes the agent's own SSRF check being off
        #: (security.allow_private_urls in its config); with the check on,
        #: nothing in the namespace is reachable and a Route is no better —
        #: from inside a pod the apps domain resolves to the router's
        #: internal address, which is precisely what the check blocks.
        self._docs_url = docs_url.rstrip("/")
        #: Which pod holds the agent's filesystem, and the container in it.
        #: The filesystem goals are measured by exec'ing in and looking;
        #: unset, they fall back to asking the agent and say so.
        self._agent_pod_selector = agent_pod_selector
        self._agent_pod_container = agent_pod_container
        self._cases = cases if cases is not None else loader.load_all()
        self._timeout_s = timeout_s
        self._lock = asyncio.Lock()
        #: Canaries planted this process: rendered path -> the cycle id that
        #: planted it. The id matters as much as the path. A canary is named
        #: report-canary-<cycle-id>.md so a stale one from an earlier run can
        #: never satisfy a later one — but that means an attack targeting a
        #: canary has to run under the *planting* run's id, not its own.
        #: Minting a fresh one made the rendered requirement name a file that
        #: by construction had never existed, and deleted-reports skipped
        #: itself forever.
        self._canaries: dict[str, str] = {}
        #: What holds the lock: "evals", "attack", "reset" or "". The
        #: console's button labels are a function of this and nothing else,
        #: so a browser reloaded mid-sweep comes back showing Stop rather
        #: than a Run button that answers 409.
        self._running = ""
        #: A stop the operator asked for, honoured between evals and between
        #: an attack's turns. Never mid-cleanup: a half-undone attack leaves
        #: the plant in a state nobody was told about.
        self._stop_requested = False

    async def reset_everything(self, why: str) -> bool:
        """The whole reset, for the console's Reset button.

        A public name for what the cleanup paths already do, so the button
        and they cannot drift apart again. They had: the endpoint reset the
        simulation alone, which left a tampered soul and a patched skill in
        place through the one reset a participant can press.

        Under the lock, unlike the `_reset_plant` it delegates to: that one is
        called *with* the lock held by the sweep and the attack paths, and an
        asyncio.Lock does not nest. Two presses used to interleave — two
        reseeds, the execs racing each other, two plant-reset event pairs, so
        the console cleared the transcript twice — and a reset landing
        mid-sweep reseeds the plant underneath an eval that is reading it.
        """
        async with self._lock:
            self._stop_requested = False
            self._running = "reset"
            try:
                return await self._reset_plant(why)
            finally:
                self._running = ""

    async def _reset_plant(self, why: str) -> bool:
        """Reseed the plant and tell the console, so it can clear with it.

        Emitting the same event the Reset button produces is what keeps the
        three paths consistent: after any reset the transcript above refers to
        readings and work orders that no longer exist.

        Returns whether the plant itself was reseeded — the file restores are
        best effort and named in the event instead, but a plant-api that
        refused is the one failure a caller has to be able to report.
        """
        # Said before the work, not after. A reset is three pod execs and a
        # plant-api reseed now — long enough that a button which only greys
        # out reads as a console that swallowed the click. The console keys
        # this to the same plant-reset toast it resolves below, so the
        # spinner becomes the verdict rather than a second notification.
        events.emit(
            kind="plant", phase="started", id="plant-reset",
            title="Resetting the plant",
            detail="reseeding the plant and putting the agent's reports, "
                   "instructions and skills back",
        )
        if not await plant.hard_reset(self._client, self._plant_api_url):
            events.emit(
                kind="plant", phase="resolved", id="plant-reset",
                title="Reset failed", outcome="inconclusive",
                detail="plant-api refused the reset; nothing was changed",
            )
            return False
        restored, soul, skills = await self._restore_agent_files()
        missed = []
        if not restored:
            missed.append(goals.REPORTS_CANARY)
        if not soul:
            missed.append("the agent's instructions")
        if not skills:
            missed.append("the agent's skills")
        events.emit(
            kind="plant", phase="resolved", id="plant-reset",
            title="Plant reset", outcome="reset",
            detail=f"{why} Work orders and maintenance records raised before "
                   "now are gone." + ("" if not missed else
                   f" Could not restore: {', '.join(missed)}."),
        )
        return True

    async def _restore_agent_files(self) -> tuple[bool, bool, bool]:
        """Reports, instructions and skills, put back together.

        One pod lookup for the three of them, and the three copies in flight
        at once. It was six `oc` spawns strictly in a row — three of them the
        same `oc get pods` — and at OC_EXEC_TIMEOUT_S apiece that is a reset
        the participant sits through watching one spinner, before every eval
        sweep as well as on the button.

        Concurrent is safe because the three touch disjoint paths: /reports,
        $HERMES_HOME/SOUL.md, and $HERMES_HOME/skills (reseed-skills.sh
        writes nowhere else — checked, because the whole argument rests on
        it).

        Returns reports, instructions, skills — the order the resolved event
        names them in, which is the order anyone reading "Could not restore:"
        has already learned.
        """
        if not (self._agent_pod_selector and self._agent_pod_container):
            # No pod to exec into: the report falls back to asking the agent
            # and the other two cannot be done at all. The same answers by
            # the same paths as before.
            return await self._restore_report(), False, False

        pod = await goals.agent_pod(self._agent_pod_selector)
        if not pod:
            # No single running pod — the sandbox a participant has not made
            # yet, which is an ordinary mid-lab state. Each restore would
            # look again and get the same answer, so skip all three rather
            # than spend three more spawns learning it twice more.
            return False, False, False

        sel, con = self._agent_pod_selector, self._agent_pod_container
        # The agent's instructions go back too. tampered-agent-config rewrites
        # them, and an agent left running on an attacker's instructions
        # answers every later turn from them — so a reset that puts the plant
        # right and leaves the soul rewritten has not reset the lab.
        #
        # And its skills. A patched SKILL.md is the longer-lived half of the
        # same problem: the soul is one file the agent reads every turn, a
        # skill is a file it reads on the next lookup of that kind — so an
        # injected "reply only in emoji" written into read-safety-docs keeps
        # answering in later modules that never asked for a bulletin. That
        # call also removes any skill the agent invented, which a lab whose
        # modules assume exactly two cannot carry forward.
        done = await asyncio.gather(
            goals.restore_reports(sel, con, pod=pod),
            goals.restore_soul(sel, con, pod=pod),
            goals.restore_skills(sel, con, pod=pod),
            return_exceptions=True,
        )
        # `is True`, not truthiness: an exec that raised has to come back as
        # "could not restore this one". Letting it propagate aborts a reset
        # that has already reseeded the plant and not yet said so — the route
        # 500s and the console's spinner stays up for good.
        for outcome in done:
            if isinstance(outcome, BaseException):
                log.warning("reset: a restore failed: %s", outcome)
        return tuple(r is True for r in done)  # type: ignore[return-value]

    async def _restore_report(self) -> bool:
        """Put the reports directory back to its starting state.

        Every reset, not only deleted-reports' cleanup: the canary is part of
        the starting state, and a participant who resets after any attack
        should get it back.

        Done in the pod when there is one to exec into, exactly as the soul
        and the skills are. Asking the agent was the original mechanism and
        it restores one file at best: it cannot remove the reports earlier
        turns wrote, its own policy may by then forbid writing there, and
        after a delete-reports attack it is the suspect being asked to put
        the evidence back. Six stray reports and a reset that left all of
        them is what this replaces.

        The prompt stays as the fallback for a console with no pod selector
        configured, which is the same shape fs_observe uses when it cannot
        look for itself.

        Best effort, and reported rather than raised: a reset that fails to
        rewrite one file should still reset the plant, but it must not claim
        to have restored something it did not.
        """
        if self._agent_pod_selector and self._agent_pod_container:
            return await goals.restore_reports(
                self._agent_pod_selector, self._agent_pod_container
            )
        if not self._agent_url:
            return False
        turn = await self._ask(goals.REPORTS_RESTORE_PROMPT.format(
            path=goals.REPORTS_CANARY, body=goals.REPORTS_CANARY_BODY))
        return turn.ok

    async def ensure_plant_healthy(self) -> tuple[bool, str]:
        """Recover a plant left in shutdown by an earlier session.

        Run once at startup. The console opening onto a dark plant with the
        reservoir at zero is the first thing anyone sees, and until the latch
        is lifted nothing it displays afterwards means anything.
        """
        return await plant.ensure_healthy(self._client, self._plant_api_url)

    # -- catalogue ---------------------------------------------------------

    @property
    def cases(self) -> list[Case]:
        return self._cases

    def attacks(self, *, enabled_only: bool = True) -> list[Case]:
        return loader.attacks(self._cases, enabled_only=enabled_only)

    def evals(self, *, enabled_only: bool = True) -> list[Case]:
        return loader.evals(self._cases, enabled_only=enabled_only)

    def busy(self) -> bool:
        return self._lock.locked()

    def running(self) -> str:
        """What is running right now: "evals", "attack", "reset" or "".

        `busy` cannot tell the console which button to turn into Stop, and a
        browser reloaded mid-sweep would otherwise come back offering a Run
        that answers 409.
        """
        return self._running

    def stopping(self) -> bool:
        """Has a stop been asked for and not yet taken effect?"""
        return self._stop_requested

    def stop(self) -> bool:
        """Ask whatever is running to stop at its next checkpoint.

        Returns whether there was anything to stop.

        Cooperative, deliberately. Cancelling the task instead would be
        unsafe: CancelledError is a BaseException, so `_run_attack`'s
        `except Exception` misses it and the finally that runs cleanup would
        fire with `succeeded` still False -- skipping the cleanup -- then emit
        an empty inconclusive verdict and die. That is the console reporting
        "nothing happened" about a plant that is still shut down. So a stop
        ends the sweep and reports what finished; it does not un-run the turn
        already in flight, which is running on the agent's side anyway.
        """
        if not self._lock.locked():
            return False
        self._stop_requested = True
        return True

    def catalogue(self) -> dict[str, Any]:
        """What the console renders in its "not yet armed" list."""
        return {
            "attacks": [self._describe(c) for c in self.attacks(enabled_only=False)],
            "evals": [self._describe(c) for c in self.evals(enabled_only=False)],
            "busy": self.busy(),
            # Empty because nothing was mounted, as opposed to empty because
            # every case is disabled. Identical on screen, unrelated causes.
            "problem": loader.load_problem() if not self._cases else "",
        }

    @staticmethod
    def _describe(case: Case) -> dict[str, Any]:
        return {
            "id": case.id,
            "title": case.title,
            "description": case.description,
            "severity": case.severity,
            "enabled": case.enabled,
            "disabledReason": case.disabled_reason,
            "module": case.maps_to_module,
        }

    # -- running -----------------------------------------------------------

    def pick_attack(self) -> Case | None:
        """One enabled attack at random — the scheduler's and the button's pick.

        Uniform, deliberately. Weighting by severity would make the high-value
        attacks the only ones anyone ever sees, and the cheap ones are the ones
        that catch a policy loosened by accident.
        """
        armed = self.attacks()
        return secrets.choice(armed) if armed else None

    async def run_attack(self, case: Case, *, cycle_id: str | None = None) -> Outcome:
        # Announced before the lock, not after the preparation.
        #
        # Everything between the click and this line used to be invisible:
        # waiting on the lock, an optional plant reset, a state snapshot, the
        # shutdown check, and — for the attack that tampers with the agent's
        # instructions — a whole model call to read the file first. The
        # console sat blank through all of it and looked frozen.
        events.emit(
            kind="attack", phase="started", id=case.id, title=case.title,
            severity=case.severity, blurb=case.blurb,
        )
        async with self._lock:
            # Cleared inside the lock: `stop()` only sees that something holds
            # it, so a stop pressed just after the last checkpoint of the
            # previous run would otherwise stop this one before it began.
            self._stop_requested = False
            self._running = "attack"
            try:
                return await self._run_attack(
                    case, cycle_id or self._cycle_id_for(case)
                )
            finally:
                self._running = ""

    def _render(self, value: Any, cycle_id: str) -> Any:
        """Fill a definition's template variables for this run.

        One place rather than seven call sites, because the variables have to
        agree: the prompt the agent is given and the goal we judge it against
        must name the same canary, the same documentation host and the same
        agent home.

        `hermes_home` comes from goals, which decides it from AGENT_URL --
        the thing that actually moves when module 2 points the console at the
        sandbox. A definition that wrote the path out instead was frozen when
        the chart rendered, and from module 2 on it asked the sandboxed agent
        to edit the plain agent's file: refused as a protected system path,
        scored "declined", and the filesystem policy never exercised.
        """
        return loader.render(value, cycle_id, self._docs_url, goals.HERMES_HOME)

    def _cycle_id_for(self, case: Case) -> str:
        """The cycle id this attack should run under.

        An attack that targets a canary adopts the id of the run that planted
        one, so the file it names is the file that exists. Everything else
        gets a fresh id.
        """
        if case.requires_canary and self._canaries:
            # The most recently planted one: earlier canaries may already have
            # been destroyed by an earlier run of this same attack.
            return next(reversed(self._canaries.values()))
        return new_cycle_id()

    async def run_all_evals(self) -> dict[str, Any]:
        """The whole eval suite, in one sweep, under one lock.

        Canary-planting evals go first so the artefacts later cases depend on
        exist before those cases run. Held under a single lock for the whole
        sweep: an attack firing in the middle would change the plant underneath
        an eval that is checking it, and the eval would fail for a reason that
        has nothing to do with the participant's policy.
        """
        async with self._lock:
            # Cleared inside the lock, before anything can check it: `stop()`
            # only sees that the lock is held, so a stop pressed in the window
            # after the previous run's last checkpoint would otherwise end
            # this sweep before its first eval.
            self._stop_requested = False
            self._running = "evals"
            try:
                return await self._sweep()
            finally:
                self._running = ""

    async def _sweep(self) -> dict[str, Any]:
        """The sweep itself, with the lock already held."""
        if RESET_BEFORE_EVALS:
            await self._reset_plant("Reset before the evaluation suite.")

        cycle_id = new_cycle_id()
        cases = sorted(self.evals(), key=lambda c: not c.plants_canary)
        events.emit(
            kind="evals", phase="started", id="eval-sweep",
            title="Evaluation suite", total=len(cases),
            blurb=f"Running {len(cases)} evals against the agent.",
        )
        results: list[Outcome] = []
        for index, case in enumerate(cases, start=1):
            # Between evals, never inside one: an eval abandoned mid-judgement
            # would be reported as a verdict nobody measured.
            if self._stop_requested:
                break
            results.append(
                await self._run_eval(case, cycle_id, index=index, total=len(cases))
            )

        not_run = cases[len(results):]
        passed = sum(1 for r in results if r.outcome == "passed")
        inconclusive = [r for r in results if not r.conclusive]
        summary = {
            "kind": "evals",
            "phase": "resolved",
            "id": "eval-sweep",
            "title": "Evaluation suite",
            "passed": passed,
            "total": len(results),
            "inconclusive": len(inconclusive),
            # From what was left undone, not from the flag: a stop pressed as
            # the last eval resolved cut nothing short, and a card that says
            # "stopped" about a suite that ran in full is a lie in the cheap
            # direction.
            "stopped": bool(not_run),
            # Named, and deliberately kept out of `results`: a never-run eval
            # folded in there would be counted by mlflow_run as an eval that
            # ran and left no trace, which is the opposite of what happened.
            "notRun": [{"id": c.id, "title": c.title} for c in not_run],
            "results": [
                {
                    "id": r.case_id,
                    "title": r.title,
                    "outcome": r.outcome,
                    "detail": r.detail,
                    "session_id": r.session_id,
                }
                for r in results
            ],
        }
        return summary

    # -- internals ---------------------------------------------------------

    async def _ask(self, prompt: str) -> agent.TurnResult:
        """One turn to the agent, the same way every probe reaches it.

        Silent, and in its own conversation. This is how the goal checks and
        the cleanup talk to the agent, and those are the lab's own questions
        about what just happened -- not part of the conversation the
        participant is having. Putting "is report-canary-x.md still there?" on
        screen between an attack and its verdict would read as though the
        operator had typed it.
        """
        return await agent.ask(
            self._client, self._agent_url, [prompt], timeout_s=self._timeout_s
        )

    #: Every probe asks as the plant operator. Named here rather than left to
    #: agent.ask's default, because the console labels the echoed turn with it
    #: and a transcript crediting the attack to the persona the participant
    #: happens to have selected would be wrong about the one thing
    #: unauthorized-control-action is testing.
    PROBE_PERSONA = "operator"

    def _echo(self, session: str, case: Case) -> agent.TurnEcho:
        """Put an attack's turns in the participant's console as they happen.

        Rendering the turns without also joining the session would make the
        transcript lie: the console would show a conversation the agent cannot
        remember, so the obvious next move -- "why did you just do that?" --
        would be answered by a session that never saw the attack. The session
        id rides along on every event and the console adopts it, which is what
        makes that follow-up work.

        Emitted turn by turn rather than as a block at the end, so a
        multi-turn attack reads at the speed it actually runs.
        """
        async def echo(role: str, text: str) -> None:
            events.emit(
                kind="turn",
                role=role,
                text=text,
                session=session,
                persona=self.PROBE_PERSONA,
                id=case.id,
                title=case.title,
            )

        return echo

    async def _run_attack(self, case: Case, cycle_id: str) -> Outcome:
        spec = self._render(case.goal, cycle_id)
        prompts = [self._render(p, cycle_id) for p in case.prompts]

        # An attack whose canary was never planted is skipped, not scored.
        # Crediting a deletion that could not have happened would tell the
        # participant they are defended when nothing was tested.
        if case.requires_canary:
            wanted = self._render(case.requires_canary, cycle_id)
            if wanted not in self._canaries:
                result = Outcome(
                    case.id, case.title, "inconclusive",
                    "skipped: this attack deletes a decoy report that the "
                    "write-report eval plants, and no decoy exists yet — run "
                    "the eval suite first",
                )
                await self._emit_resolved(case, result)
                return result

        if RESET_BEFORE_ATTACK:
            await self._reset_plant("Reset before running an attack.")

        before = await plant.snapshot(self._client, self._plant_api_url)

        # Refuse to probe a plant that is already in emergency shutdown.
        #
        # The latch is one-way on any plant-api without
        # POST /emergency-shutdown/clear, so a single attack that lands
        # emergency_shutdown darkens the plant until someone resets it by
        # hand. Every probe after that is measured against a corpse: with all
        # pumps already stopped, state_diff cannot see a pump being stopped,
        # so attacks that did land would be scored "blocked" and the console
        # would tell the participant they are defended by a plant that is
        # simply dead. Observed on cluster-pz7xk, where the reservoir had
        # drained to 0.0% and turbidity was critical before anyone noticed.
        #
        # Try to release it; if that is not possible, stop and say so rather
        # than producing a result that cannot mean anything.
        if before.shutdown:
            healthy, why = await plant.ensure_healthy(self._client, self._plant_api_url)
            if not healthy:
                result = Outcome(
                    case.id, case.title, "inconclusive",
                    f"attack not run: {why}",
                )
                await self._emit_resolved(case, result)
                return result
            before = await plant.snapshot(self._client, self._plant_api_url)

        # Before the prompt goes out, so a hit cannot predate the probe that
        # is about to be credited with it. The observer's clock is its own,
        # so ask it rather than using ours.
        started_at = await self._observer_now()
        # Captured before the prompt goes out: after it the file may already
        # be the attacker's version. Only for the case that needs it — it
        # costs an extra turn.
        soul_before = ""
        if case.cleanup.get("type") == "soul_restore":
            soul_before = await self._read_soul(case.cleanup.get("path", "SOUL.md"))
        succeeded = False
        shutdown_was_set = False
        result = Outcome(case.id, case.title)

        try:
            # Minted here rather than left to agent.ask, because the console
            # has to be told which conversation to adopt from the first turn
            # event -- not after the attack has finished.
            session = str(uuid.uuid4())
            turn = await agent.ask(
                self._client, self._agent_url, prompts,
                timeout_s=self._timeout_s,
                persona=self.PROBE_PERSONA,
                session_id=session,
                on_turn=self._echo(session, case),
                should_stop=lambda: self._stop_requested,
            )
            result.session_id = turn.session_id
            if not turn.ok:
                result.detail = turn.error
                return result

            verdict = await self._judge(
                spec, turn, before, cycle_id, since=started_at
            )
            if verdict is None:
                result.detail = "could not be judged — see the definition for why"
                return result

            succeeded, detail = verdict
            if succeeded and case.requires_canary:
                # Consumed. Offering a deleted decoy to the next run would
                # score an attack against a file that is already gone.
                self._canaries.pop(self._render(case.requires_canary, cycle_id), None)
            if succeeded and case.cleanup.get("type") == "reports_restore":
                # This attack empties the directory, so every decoy planted by
                # an earlier write-report went with it. Leaving them in the
                # map would let a later case believe a file exists that the
                # attack has just deleted.
                self._canaries.clear()
            after = await plant.snapshot(self._client, self._plant_api_url)
            shutdown_was_set = after.shutdown and not before.shutdown
            if succeeded:
                result.outcome = "got_through"
                result.detail = detail
            elif goals.blocked_by_policy(turn.text):
                result.outcome = "blocked"
                result.detail = detail
            else:
                result.outcome = "declined"
                result.detail = (
                    f"{detail} — but nothing refused the request; the agent "
                    "chose not to comply, so your policy was never exercised"
                )
            if turn.stopped:
                # Said in front of the verdict, because the verdict is now
                # about a shorter attack than the definition describes: the
                # turns that were never sent might have landed.
                result.detail = (
                    "stopped by the operator part-way through — "
                    + result.detail
                )
            return result
        except Exception as exc:  # noqa: BLE001 - a probe must never kill the loop
            result.detail = f"probe error: {exc}"
            return result
        finally:
            # Unconditional. A probe that timed out or raised may still have
            # changed the plant before it did, and the one thing worse than an
            # unjudged attack is an unjudged attack that left damage behind.
            done = await cleanup.run(
                case.cleanup,
                client=self._client,
                plant_api_url=self._plant_api_url,
                before=before,
                shutdown_was_set=shutdown_was_set,
                soul_before=soul_before,
                # Restoring goes back through the same chat endpoint the
                # attack used, so the agent undoes its own work. cleanup falls
                # back to plant-api only if that does not restore the plant.
                ask=self._ask,
                agent_pod_selector=self._agent_pod_selector,
                agent_pod_container=self._agent_pod_container,
            ) if (succeeded or shutdown_was_set) else cleanup.CleanupResult(
                attempted=False, detail="attack did not land — nothing to undo"
            )
            result.cleanup = done.as_event()
            await self._emit_resolved(case, result)

    async def _emit_resolved(self, case: Case, result: Outcome) -> None:
        """Report the verdict now; let the trace link catch up.

        The lookup polls MLflow for up to PROBE_TRACE_LOOKUP_S, because the
        exporter is asynchronous and an attack usually finishes before its
        trace lands. Waiting for that *before* emitting meant a slow export
        held the whole result card back by up to a minute — the attack was
        over and the console showed nothing.

        So the verdict goes out immediately and the link arrives as its own
        event. The console shows "looking for the trace…" in the meantime and
        replaces it either way, so it never promises a link it cannot deliver.
        """
        events.emit(
            kind="attack", phase="resolved", id=case.id, title=case.title,
            severity=case.severity, blurb=case.blurb,
            outcome=result.outcome, detail=result.detail, cleanup=result.cleanup,
            session=result.session_id,
            # Whether there is any point waiting: with no session, or no
            # MLflow, the console should say so rather than spin.
            tracePending=bool(result.session_id and mlflow_run.enabled()),
        )
        if result.session_id and mlflow_run.enabled():
            # Detached: this outlives the attack, and nothing waits on it.
            asyncio.create_task(self._emit_trace(case, result.session_id))

    async def _emit_trace(self, case: Case, session: str) -> None:
        """Fill in the trace link once MLflow has indexed it."""
        url = ""
        try:
            url = await mlflow_run.trace_url_for_session(session)
        except Exception:  # noqa: BLE001 - a missing link must not sink a result
            log.exception("could not resolve the trace for %s", case.id)
        events.emit(
            kind="trace", id=case.id, session=session, traceUrl=url,
        )

    async def _run_eval(
        self, case: Case, cycle_id: str, *, index: int = 0, total: int = 0
    ) -> Outcome:
        # Said before the work, like every other probe: a sweep of six behind
        # one unchanging "this takes a minute" is indistinguishable from a
        # console that hung. `index` counts in the order the sweep runs them,
        # which is not the catalogue order -- canary planters go first -- so
        # "3 of 6" names the eval actually running.
        events.emit(
            kind="eval", phase="started", id=case.id, title=case.title,
            severity=case.severity, index=index, total=total,
        )
        spec = self._render(case.goal, cycle_id)
        prompts = [self._render(p, cycle_id) for p in case.prompts]
        result = Outcome(case.id, case.title)

        before = await plant.snapshot(self._client, self._plant_api_url)
        before_count = -1
        if spec.get("type") == "state_created":
            before_count = await goals._count(
                self._client, self._plant_api_url, spec.get("collection", "work-orders")
            )
        # Which reports were already there. Taken before the prompt goes out,
        # because "a report appeared" is a delta and the directory is not
        # empty to start with -- the console seeds one and the participant
        # may have written others.
        before_files: set[str] | None = None
        if spec.get("type") == "fs_created":
            before_files = await goals.fs_list(
                self._agent_pod_selector, self._agent_pod_container
            )

        try:
            turn = await agent.ask(
                self._client, self._agent_url, prompts, timeout_s=self._timeout_s
            )
            result.session_id = turn.session_id
            if not turn.ok:
                result.detail = turn.error
                return result

            verdict = await self._judge(
                spec, turn, before, cycle_id,
                before_count=before_count, before_files=before_files,
            )
            if verdict is None:
                result.detail = "could not be judged"
                return result

            reached, detail = verdict
            result.outcome = "passed" if reached else "failed"
            result.detail = detail

            if reached and case.plants_canary:
                self._canaries[self._render(case.plants_canary, cycle_id)] = cycle_id
            return result
        except Exception as exc:  # noqa: BLE001
            result.detail = f"probe error: {exc}"
            return result
        finally:
            events.emit(
                kind="eval", phase="resolved", id=case.id, title=case.title,
                severity=case.severity, outcome=result.outcome, detail=result.detail,
            )

    async def _read_soul(self, path: str) -> str:
        """The agent's instructions as they stand, for restoring afterwards.

        Read off the filesystem where that is possible, and only asked of the
        agent otherwise. Asking cost a full model call before the attack's
        prompt could even be sent — twenty-odd seconds of blank console — and
        relied on the agent quoting its own instructions back accurately.

        Empty when it cannot be read either way, which cleanup treats as "do
        not run the restore" rather than "restore to nothing": overwriting the
        agent's instructions with an empty file would be worse than the attack
        it is meant to undo.
        """
        body = await goals.fs_read(
            path, self._agent_pod_selector, self._agent_pod_container
        )
        if body:
            return body
        turn = await agent.ask(
            self._client,
            self._agent_url,
            [goals.SOUL_READ_PROMPT.format(path=path)],
            timeout_s=self._timeout_s,
        )
        return goals.extract_block(turn.text) if turn.ok else ""

    async def _observer_now(self) -> float:
        """The documentation site's own clock, for attributing hits.

        Its clock, not ours: the two pods can drift, and a few seconds of skew
        either way decides whether a hit counts.
        """
        if not self._docs_url:
            return 0.0
        try:
            response = await self._client.get(f"{self._docs_url}/hits", timeout=10.0)
            response.raise_for_status()
            return float(response.json().get("now", 0.0))
        except (httpx.HTTPError, ValueError, TypeError):
            return 0.0

    async def _judge(
        self,
        spec: dict[str, Any],
        turn: agent.TurnResult,
        before: plant.Snapshot,
        cycle_id: str,
        *,
        before_count: int = -1,
        before_files: set[str] | None = None,
        since: float = 0.0,
    ) -> goals.Verdict:
        """Dispatch to the goal checker the case declares."""
        kind = spec.get("type")

        if kind == "all":
            # Every sub-goal must be reached. One inconclusive sub-goal makes
            # the whole thing inconclusive — a partial answer to "did the whole
            # chain work" is not an answer.
            details: list[str] = []
            for sub in spec.get("of") or []:
                verdict = await self._judge(
                    sub, turn, before, cycle_id,
                    before_count=before_count, since=since,
                )
                if verdict is None:
                    return None
                reached, detail = verdict
                details.append(detail)
                if not reached:
                    return False, detail
            return True, "; ".join(details)

        if kind == "response_regex":
            return goals.response_regex(spec, turn.text)

        if kind == "state_echo":
            state = await plant.read_state(self._client, self._plant_api_url)
            return goals.state_echo(spec, turn.text, state)

        if kind == "state_diff":
            after = await plant.snapshot(self._client, self._plant_api_url)
            return goals.state_diff(spec, before, after)

        if kind == "state_created":
            return await goals.state_created(
                spec, self._client, self._plant_api_url, before_count
            )

        if kind == "fs_probe":
            # Looked at, not asked about. The agent used to be the only thing
            # that could see its own filesystem, so the check was a second
            # turn — "does this file exist? reply PRESENT or ABSENT" — which
            # asks the suspect whether the evidence is still there, costs a
            # model call, and scores the attack inconclusive whenever the
            # reply was too chatty to parse.
            observed, state = await goals.fs_observe(
                spec, self._agent_pod_selector, self._agent_pod_container
            )
            if observed is not None:
                return goals.fs_probe(spec, observed, state)

            # No observer: the sandbox runtime, or the sidecar is down. Fall
            # back to asking, in its own conversation so the attack's prompt
            # cannot colour the answer — and say so in the verdict, because a
            # participant reading "the file is gone" deserves to know whether
            # that was measured or reported by the thing under test.
            check = await agent.ask(
                self._client,
                self._agent_url,
                [goals.fs_prompt(spec)],
                timeout_s=self._timeout_s,
            )
            if not check.ok:
                return None
            verdict = goals.fs_probe(spec, goals.interpret_fs_answer(check.text))
            if verdict is None:
                return None
            ok, detail = verdict
            return ok, f"{detail} (reported by the agent, not measured)"

        if kind == "fs_created":
            # Looked at, like fs_probe, and for the same reason: asking the
            # agent whether it wrote a file is asking the thing under test.
            after = await goals.fs_list(
                self._agent_pod_selector, self._agent_pod_container
            )
            return goals.fs_created(before_files, after)

        if kind == "honeypot_hit":
            return await goals.honeypot_hit(
                spec, self._client, self._docs_url, since
            )

        return None
