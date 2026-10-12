"""The randomised attack timer.

An in-process asyncio task rather than a CronJob, for the reason the token
refresher gives next door: a single long-running loop can hold the per-probe
plant snapshot across the attack/cleanup pair without persisting it anywhere.

The interval is random inside a window rather than fixed. A fixed five-minute
tick is learnable — a participant notices the rhythm and watches the clock
instead of the console — and the point is that an attack arrives while they are
in the middle of something else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from .engine import Engine

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, engine: Engine, *, min_s: float, max_s: float) -> None:
        self._engine = engine
        self._min_s = min_s
        self._max_s = max(min_s, max_s)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="attack-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def _delay(self) -> float:
        return random.uniform(self._min_s, self._max_s)

    async def _loop(self) -> None:
        # Wait before the first one. A console that is attacked the instant it
        # loads reads as a broken console, not as an attack.
        await asyncio.sleep(self._delay())
        while True:
            try:
                if self._engine.busy():
                    # Someone is already running something from a button. Skip
                    # this slot rather than queueing: a queued attack lands at
                    # an unrelated moment and the toast makes no sense.
                    log.info("attack slot skipped — a probe is already running")
                else:
                    case = self._engine.pick_attack()
                    if case is not None:
                        log.info("scheduled attack: %s", case.id)
                        await self._engine.run_attack(case)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must outlive any one probe
                log.exception("scheduled attack failed")
            await asyncio.sleep(self._delay())
