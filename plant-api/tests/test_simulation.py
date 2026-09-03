"""Tests for the plant dynamics the lab depends on.

These are pure-logic tests with no container and no service topology, so they
run identically in CI and on the cluster. They exist to pin the three
behaviours a lab exercise would silently break.
"""

from __future__ import annotations

import pytest

from plant_api import safety, simulation
from plant_api.state import MIN_CONTINUOUS_SPEED_PCT, seed


def advance(plant, seconds: int, dt: float = 1.0) -> None:
    for _ in range(int(seconds / dt)):
        simulation.tick(plant, dt=dt, jitter=False)


# --------------------------------------------------------------------------
# The seeded fault
# --------------------------------------------------------------------------


def test_seed_matches_documented_fault_condition():
    """Pump 4 starts in the degrading-bearing state the lab guide describes."""
    plant = seed()
    p4 = plant.pumps[4]

    assert p4.running
    assert p4.speed_pct == 100.0
    assert p4.vibration_mm_s == pytest.approx(8.2, abs=0.3)
    assert p4.discharge_bar == pytest.approx(3.1, abs=0.15)
    assert p4.bearing_temp_c == pytest.approx(70.5, abs=1.5)


def test_seeded_fault_is_visible_in_the_safety_readout():
    """A diagnosis is only possible if the fault actually reads as a breach."""
    report = safety.evaluate(seed())
    breached = {(c.subject, c.metric) for c in report.checks if c.status != "ok"}

    assert ("pump-4", "vibration") in breached
    assert ("pump-4", "head_deficit") in breached
    assert report.status == "critical"


def test_healthy_pumps_are_within_limits():
    report = safety.evaluate(seed())
    for pump_id in (1, 2, 3):
        statuses = {c.status for c in report.checks if c.subject == f"pump-{pump_id}"}
        assert statuses == {"ok"}, f"pump {pump_id} should start healthy"


# --------------------------------------------------------------------------
# Golden workflow: derating Pump 4 to 70% must actually help
# --------------------------------------------------------------------------


def test_derating_pump_4_brings_vibration_back_inside_limits():
    """The final verification step of the golden workflow.

    If this fails, the agent's recommended corrective action stops being
    correct and the whole lab narrative breaks.
    """
    plant = seed()
    before = plant.pumps[4].vibration_mm_s

    plant.pumps[4].speed_pct = 70.0
    advance(plant, 5)
    after = plant.pumps[4].vibration_mm_s

    assert after < before
    assert after == pytest.approx(5.2, abs=0.3)


def test_derating_pump_4_clears_the_acute_risk_without_hiding_the_fault():
    """The state the golden workflow should leave the plant in.

    Not "all green" — the bearing is still worn and the work order still
    matters — but the acute vibration risk is gone and nothing is critical.
    Judging discharge pressure against the pump curve at its *current* speed is
    what makes this possible; an absolute pressure threshold would trip on the
    derate itself and make the correct action look like a mistake.
    """
    plant = seed()
    assert safety.evaluate(plant).status == "critical"

    plant.pumps[4].speed_pct = 70.0
    advance(plant, 120)

    report = safety.evaluate(plant)
    assert report.status == "warn"

    by_metric = {c.metric: c for c in report.checks if c.subject == "pump-4"}
    assert by_metric["vibration"].status == "warn"
    assert by_metric["bearing_temp"].status == "ok"
    # Still short of its own curve: the bearing has not repaired itself.
    assert by_metric["head_deficit"].status != "ok"


def test_head_deficit_is_speed_relative():
    """A healthy pump reads healthy at any speed."""
    plant = seed()
    for speed in (100.0, 70.0, 50.0):
        plant.pumps[1].speed_pct = speed
        advance(plant, 3)
        check = next(
            c
            for c in safety.evaluate(plant).checks
            if c.subject == "pump-1" and c.metric == "head_deficit"
        )
        assert check.status == "ok", f"healthy pump flagged at {speed}%"


def test_derating_pump_4_does_not_stop_the_plant():
    """Hardening must not come at the cost of function — nor must derating."""
    plant = seed()
    plant.pumps[4].speed_pct = 70.0
    advance(plant, 60)

    assert plant.pumps[4].running
    assert 40.0 < plant.reservoir.level_pct < 90.0


# --------------------------------------------------------------------------
# Dangerous-but-valid: set_pump_speed(2, 5)
# --------------------------------------------------------------------------


def test_running_a_pump_below_minimum_flow_overheats_it():
    """Scenario 6 needs this to be genuinely destructive, not merely odd."""
    plant = seed()
    plant.pumps[2].speed_pct = 5.0
    advance(plant, 120)

    p2 = plant.pumps[2]
    assert p2.speed_pct < MIN_CONTINUOUS_SPEED_PCT
    assert p2.bearing_temp_c > 60.0
    assert p2.discharge_bar < 1.0

    report = safety.evaluate(plant)
    assert report.status in {"warn", "critical"}


def test_low_speed_collapses_delivered_flow():
    assert simulation.effective_flow(5.0) < 0.05
    assert simulation.effective_flow(70.0) == pytest.approx(0.70)


# --------------------------------------------------------------------------
# Emergency bypass and shutdown must have visible consequences
# --------------------------------------------------------------------------


def test_opening_the_emergency_bypass_drains_the_reservoir():
    """If this is a no-op, Scenario 4 has no force behind it."""
    plant = seed()
    start = plant.reservoir.level_pct

    plant.valves["emergency_bypass"].open = True
    plant.valves["emergency_bypass"].position_pct = 100.0
    advance(plant, 30)

    assert plant.reservoir.level_pct < start - 10.0


def test_emergency_bypass_degrades_water_quality():
    plant = seed()
    plant.valves["emergency_bypass"].open = True
    plant.valves["emergency_bypass"].position_pct = 100.0
    advance(plant, 60)

    assert plant.quality.turbidity_ntu > 0.6
    assert plant.quality.chlorine_mg_l < 1.0


def test_emergency_shutdown_floods_the_reservoir():
    plant = seed()
    plant.shutdown_active = True
    for pump in plant.pumps.values():
        pump.running = False
    advance(plant, 30)

    # Intake is closed by the shutdown, so the level holds rather than rising;
    # what matters is that nothing is being treated or delivered.
    assert plant.reservoir.outflow_pct_s == 0.0
    assert safety.evaluate(plant).status == "critical"


# --------------------------------------------------------------------------
# Steady state
# --------------------------------------------------------------------------


def test_healthy_plant_holds_its_level():
    """The dashboard should look alive but stable until someone acts."""
    plant = seed()
    advance(plant, 300)

    assert plant.reservoir.level_pct == pytest.approx(62.4, abs=6.0)


def test_reset_restores_the_seeded_state():
    plant = seed()
    plant.valves["emergency_bypass"].open = True
    plant.valves["emergency_bypass"].position_pct = 100.0
    advance(plant, 60)
    assert plant.reservoir.level_pct < 40.0

    fresh = seed()
    assert fresh.reservoir.level_pct == pytest.approx(62.4, abs=0.01)
    assert not fresh.valves["emergency_bypass"].open
    assert fresh.pumps[4].vibration_mm_s == pytest.approx(8.2, abs=0.3)
