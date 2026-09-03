"""Safety envelope readout.

This module reports whether the plant is inside safe operating bounds. It never
blocks anything. Enforcement is deliberately absent: in this lab, authorization
belongs to MCP Gateway and execution policy belongs to OpenShell. A plant that
refused unsafe commands on its own would quietly do the job the attendee is
supposed to make the infrastructure do.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .simulation import expected_discharge_bar
from .state import (
    BEARING_CRIT_C,
    BEARING_WARN_C,
    HEAD_DEFICIT_CRIT_PCT,
    HEAD_DEFICIT_WARN_PCT,
    LEVEL_HIGH_CRIT_PCT,
    LEVEL_LOW_CRIT_PCT,
    TURBIDITY_CRIT_NTU,
    VIBRATION_CRIT_MM_S,
    VIBRATION_WARN_MM_S,
    Plant,
)

Status = Literal["ok", "warn", "critical"]
_RANK: dict[Status, int] = {"ok": 0, "warn": 1, "critical": 2}


class Check(BaseModel):
    subject: str
    metric: str
    value: float
    unit: str
    status: Status
    limit: float | None = None
    note: str | None = None


class SafetyReport(BaseModel):
    status: Status
    checks: list[Check]

    @property
    def breaches(self) -> list[Check]:
        return [c for c in self.checks if c.status != "ok"]


def evaluate(plant: Plant) -> SafetyReport:
    checks: list[Check] = []

    for pump in sorted(plant.pumps.values(), key=lambda p: p.id):
        subject = f"pump-{pump.id}"
        if not pump.running:
            checks.append(
                Check(
                    subject=subject,
                    metric="state",
                    value=0.0,
                    unit="",
                    status="ok",
                    note="stopped",
                )
            )
            continue

        checks.append(
            _threshold(
                subject,
                "vibration",
                pump.vibration_mm_s,
                "mm/s",
                warn=VIBRATION_WARN_MM_S,
                crit=VIBRATION_CRIT_MM_S,
            )
        )
        checks.append(
            _threshold(
                subject,
                "bearing_temp",
                pump.bearing_temp_c,
                "degC",
                warn=BEARING_WARN_C,
                crit=BEARING_CRIT_C,
            )
        )
        checks.append(_head_deficit(subject, pump))

    level = plant.reservoir.level_pct
    if level <= LEVEL_LOW_CRIT_PCT:
        level_status: Status = "critical"
        note = "reservoir critically low — intake cannot keep up with outflow"
    elif level >= LEVEL_HIGH_CRIT_PCT:
        level_status = "critical"
        note = "reservoir overflowing — insufficient pumped outflow"
    elif level <= LEVEL_LOW_CRIT_PCT * 1.5 or level >= LEVEL_HIGH_CRIT_PCT * 0.92:
        level_status = "warn"
        note = "reservoir drifting outside normal band"
    else:
        level_status = "ok"
        note = None
    checks.append(
        Check(
            subject="reservoir",
            metric="level",
            value=level,
            unit="%",
            status=level_status,
            note=note,
        )
    )

    checks.append(
        _threshold(
            "water_quality",
            "turbidity",
            plant.quality.turbidity_ntu,
            "NTU",
            warn=TURBIDITY_CRIT_NTU * 0.7,
            crit=TURBIDITY_CRIT_NTU,
        )
    )

    if plant.valves["emergency_bypass"].open:
        checks.append(
            Check(
                subject="valve-emergency_bypass",
                metric="position",
                value=plant.valves["emergency_bypass"].position_pct,
                unit="%",
                status="critical",
                note="emergency bypass open — untreated water leaving the plant",
            )
        )

    if plant.shutdown_active:
        checks.append(
            Check(
                subject="plant",
                metric="shutdown",
                value=1.0,
                unit="",
                status="critical",
                note="emergency shutdown active — no pumps running",
            )
        )

    overall = max((c.status for c in checks), key=lambda s: _RANK[s], default="ok")
    return SafetyReport(status=overall, checks=checks)


def _head_deficit(subject: str, pump) -> Check:
    """How far short of its own pump curve this unit is running.

    Speed-relative, so derating a pump does not itself raise an alarm. A worn
    bearing shows up as a persistent deficit at every speed — which is exactly
    the signal that justifies raising a work order rather than just derating and
    walking away.
    """
    expected = expected_discharge_bar(pump.speed_pct)
    if expected <= 0:
        deficit = 0.0
    else:
        deficit = max(0.0, (expected - pump.discharge_bar) / expected * 100.0)

    status: Status = (
        "critical"
        if deficit >= HEAD_DEFICIT_CRIT_PCT
        else "warn"
        if deficit >= HEAD_DEFICIT_WARN_PCT
        else "ok"
    )
    return Check(
        subject=subject,
        metric="head_deficit",
        value=round(deficit, 1),
        unit="%",
        status=status,
        limit=HEAD_DEFICIT_WARN_PCT,
        note=(
            f"{pump.discharge_bar:.2f} bar against {expected:.2f} bar expected "
            f"at {pump.speed_pct:.0f}%"
        ),
    )


def _threshold(
    subject: str,
    metric: str,
    value: float,
    unit: str,
    *,
    warn: float,
    crit: float,
    below: bool = False,
) -> Check:
    """Classify a reading. ``below=True`` means lower values are worse."""
    if below:
        status: Status = "critical" if value <= crit else "warn" if value <= warn else "ok"
    else:
        status = "critical" if value >= crit else "warn" if value >= warn else "ok"
    return Check(
        subject=subject,
        metric=metric,
        value=value,
        unit=unit,
        status=status,
        limit=warn,
    )
