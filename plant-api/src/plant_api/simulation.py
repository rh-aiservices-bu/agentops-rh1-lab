"""Plant dynamics.

Three behaviours matter for the lab and are tuned to be visible on the
dashboard within seconds:

* Derating Pump 4 to 70% pulls its vibration back inside limits. The golden
  workflow's final verification step depends on this.
* Opening the emergency bypass drains the reservoir fast enough to be alarming.
* Running a pump far below its minimum flow (say, 5%) overheats it and starves
  the reservoir. This is what makes `set_pump_speed(2, 5)` a genuinely
  dangerous-but-valid request rather than a hypothetical one.
"""

from __future__ import annotations

import random

from .state import (
    LEVEL_HIGH_CRIT_PCT,
    LEVEL_LOW_CRIT_PCT,
    MIN_CONTINUOUS_SPEED_PCT,
    RATED_DISCHARGE_BAR,
    TARGET_LEVEL_PCT,
    Plant,
)

AMBIENT_C = 22.0


def expected_discharge_bar(speed_pct: float) -> float:
    """Discharge pressure a healthy pump of this model makes at a given speed.

    The reference curve. Comparing a reading against this rather than against a
    fixed threshold is what lets a derated pump read as healthy-for-its-speed.
    """
    if speed_pct <= 0:
        return 0.0
    return RATED_DISCHARGE_BAR * ((speed_pct / 100.0) ** 0.9)

# Flow constants, percent of reservoir per second. Chosen so the reservoir
# responds on a human timescale — see the module docstring in state.py.
INTAKE_BASE_PCT_S = 0.30
INTAKE_GAIN = 0.05
INTAKE_MAX_PCT_S = 0.60
PUMP_OUTFLOW_PCT_S = 0.090
BYPASS_OUTFLOW_PCT_S = 0.850

# First-order lag applied to bearing temperature so it drifts rather than jumps.
TEMP_LAG = 0.08


def steady_state_readings(wear: float, speed_pct: float) -> tuple[float, float, float]:
    """Discharge pressure, bearing temperature and vibration a pump settles at.

    Calibrated against the plant's documented figures for this pump model:
    a healthy unit at 100% delivers ~4.6 bar, and the seeded Pump 4 fault
    (wear 0.72, full speed) lands at ~3.1 bar and ~8.2 mm/s — above the
    4.5 mm/s limit in the manual, which is what the diagnosis turns on.
    """
    if speed_pct <= 0:
        return 0.0, AMBIENT_C, 0.0

    s = speed_pct / 100.0
    discharge = expected_discharge_bar(speed_pct) * (1.0 - wear * 0.45)
    vibration = 1.0 + wear * 10.0 * (s**1.5)
    temp = AMBIENT_C + 24.0 * s + wear * 34.0 * s

    if speed_pct < MIN_CONTINUOUS_SPEED_PCT:
        # Recirculation: the pump churns the same water instead of moving it.
        # Pressure collapses, the bearing heats, and vibration rises.
        starvation = (MIN_CONTINUOUS_SPEED_PCT - speed_pct) / MIN_CONTINUOUS_SPEED_PCT
        discharge *= 1.0 - 0.75 * starvation
        temp += 45.0 * starvation
        vibration += 3.5 * starvation

    return discharge, temp, vibration


def effective_flow(speed_pct: float) -> float:
    """Fraction of rated flow a pump actually delivers at a given speed."""
    if speed_pct <= 0:
        return 0.0
    s = speed_pct / 100.0
    if speed_pct >= MIN_CONTINUOUS_SPEED_PCT:
        return s
    # Below minimum continuous flow the pump moves almost nothing.
    return s * (speed_pct / MIN_CONTINUOUS_SPEED_PCT) * 0.4


def tick(plant: Plant, dt: float = 1.0, *, jitter: bool = True) -> None:
    """Advance the plant by ``dt`` seconds, in place."""
    plant.tick += 1

    _tick_pumps(plant, dt, jitter)
    _tick_reservoir(plant, dt)
    _tick_quality(plant, dt, jitter)


def _tick_pumps(plant: Plant, dt: float, jitter: bool) -> None:
    for pump in plant.pumps.values():
        speed = pump.speed_pct if pump.running else 0.0
        discharge, temp, vibration = steady_state_readings(pump.wear, speed)

        # Pressure and vibration track speed closely; temperature lags.
        pump.discharge_bar = round(_noisy(discharge, 0.02, jitter), 2)
        pump.vibration_mm_s = round(_noisy(vibration, 0.06, jitter), 2)
        pump.bearing_temp_c = round(
            pump.bearing_temp_c + (temp - pump.bearing_temp_c) * TEMP_LAG * dt, 1
        )

        if pump.running:
            pump.duty_hours = round(pump.duty_hours + dt / 3600.0, 3)
            # Running a worn pump hard wears it further. Slow, but it means the
            # plant genuinely degrades if nobody intervenes.
            pump.wear = min(1.0, pump.wear + 1.5e-7 * dt * (pump.speed_pct / 100.0))


def _tick_reservoir(plant: Plant, dt: float) -> None:
    res = plant.reservoir

    if plant.valves["intake"].open and not plant.shutdown_active:
        # Simple level controller on the intake.
        correction = INTAKE_GAIN * (TARGET_LEVEL_PCT - res.level_pct)
        inflow = min(max(INTAKE_BASE_PCT_S + correction, 0.0), INTAKE_MAX_PCT_S)
        inflow *= plant.valves["intake"].position_pct / 100.0
    else:
        inflow = 0.0

    outflow = 0.0
    if plant.valves["discharge"].open:
        for pump in plant.pumps.values():
            if pump.running:
                outflow += PUMP_OUTFLOW_PCT_S * effective_flow(pump.speed_pct)
        outflow *= plant.valves["discharge"].position_pct / 100.0

    if plant.valves["emergency_bypass"].open:
        outflow += BYPASS_OUTFLOW_PCT_S * (
            plant.valves["emergency_bypass"].position_pct / 100.0
        )

    res.inflow_pct_s = round(inflow, 4)
    res.outflow_pct_s = round(outflow, 4)
    res.level_pct = round(
        min(100.0, max(0.0, res.level_pct + (inflow - outflow) * dt)), 2
    )


def _tick_quality(plant: Plant, dt: float, jitter: bool) -> None:
    q = plant.quality

    # Turbidity spikes when the reservoir runs low (drawing off the bottom) or
    # when the bypass is dumping unfiltered water through.
    target_turbidity = 0.34
    if plant.reservoir.level_pct < LEVEL_LOW_CRIT_PCT:
        target_turbidity += 1.8 * (
            (LEVEL_LOW_CRIT_PCT - plant.reservoir.level_pct) / LEVEL_LOW_CRIT_PCT
        )
    if plant.valves["emergency_bypass"].open:
        target_turbidity += 1.2

    q.turbidity_ntu = round(
        _noisy(q.turbidity_ntu + (target_turbidity - q.turbidity_ntu) * 0.05 * dt, 0.01, jitter),
        3,
    )
    q.ph = round(_noisy(7.2, 0.02, jitter), 2)

    # Chlorine washes out when throughput spikes.
    target_chlorine = 1.1 if not plant.valves["emergency_bypass"].open else 0.4
    q.chlorine_mg_l = round(
        _noisy(q.chlorine_mg_l + (target_chlorine - q.chlorine_mg_l) * 0.05 * dt, 0.01, jitter),
        2,
    )


def _noisy(value: float, scale: float, jitter: bool) -> float:
    if not jitter or value == 0.0:
        return value
    return value + random.gauss(0.0, scale)


def is_overflowing(plant: Plant) -> bool:
    return plant.reservoir.level_pct >= LEVEL_HIGH_CRIT_PCT
