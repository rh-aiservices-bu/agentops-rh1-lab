"""Tests for the plant dynamics the lab depends on.

These are pure-logic tests with no container and no service topology, so they
run identically in CI and on the cluster. They exist to pin the three
behaviours a lab exercise would silently break.
"""

from __future__ import annotations

import pytest

from plant_api import safety, simulation
from plant_api.state import (
    MIN_CONTINUOUS_SPEED_PCT,
    VIBRATION_CRIT_MM_S,
    VIBRATION_WARN_MM_S,
    seed,
)


def advance(plant, seconds: int, dt: float = 1.0) -> None:
    for _ in range(int(seconds / dt)):
        simulation.tick(plant, dt=dt, jitter=False)


# --------------------------------------------------------------------------
# The seeded plant
# --------------------------------------------------------------------------


def test_the_seed_opens_with_pump_4_out_of_limits():
    """The fault the lab is built around is there from the first tick.

    ~6.5 mm/s against a 4.5 advisory limit: plainly out of limits, and the
    thing MR-2246 predicted when the drive-end bearing was replaced and the
    non-drive-end one of the same vintage was not.

    Deliberately not 0.72, which this was once. That reads ~8.2 and trips the
    7.0 critical threshold, but derating to 70% only reaches 5.2 — still over
    the limit — so the corrective action the golden workflow verifies would
    visibly fail to fix it.
    """
    plant = seed()
    p4 = plant.pumps[4]

    assert p4.running
    assert p4.speed_pct == 100.0
    assert p4.vibration_mm_s == pytest.approx(6.5, abs=0.3)
    assert p4.vibration_mm_s > VIBRATION_WARN_MM_S
    assert p4.vibration_mm_s < VIBRATION_CRIT_MM_S
    # The oldest unit on site, which is why its history is the one to ask about.
    assert p4.duty_hours == 21_680


def test_the_plant_opens_on_a_warning_not_a_crisis():
    """Amber, not red.

    A console that loads straight into CRITICAL gives a participant no way to
    tell a fault they caused from one that was always there. A warning is
    visible, diagnosable, and leaves somewhere worse to go when an attack
    lands.
    """
    report = safety.evaluate(seed())

    assert report.status == "warn"
    assert not [c for c in report.checks if c.status == "critical"]


def test_only_pump_4_is_out_of_limits():
    report = safety.evaluate(seed())

    for pump_id in (1, 2, 3):
        statuses = {c.status for c in report.checks if c.subject == f"pump-{pump_id}"}
        assert statuses == {"ok"}, f"pump {pump_id} should start sound"

    p4 = {c.metric: c.status for c in report.checks if c.subject == "pump-4"}
    assert p4["vibration"] == "warn"
    # Short of its own curve, because the bearing is worn — not because it is
    # derated. It is still at 100%.
    assert p4["head_deficit"] != "ok"


# --------------------------------------------------------------------------
# Derating a worn pump must actually help
# --------------------------------------------------------------------------
#
# The physics the golden workflow's corrective action depends on. The fault is
# injected rather than seeded now, but it has to behave the same way when it
# arrives: a pump that degrades in front of a participant is only a teachable
# moment if derating it visibly fixes the acute risk.


def _worn(wear: float = 0.72):
    """A plant whose pump 4 has degraded to the state the lab used to seed."""
    plant = seed()
    plant.pumps[4].wear = wear
    advance(plant, 5)
    return plant


def test_a_worn_pump_reads_as_a_breach():
    """A diagnosis is only possible if the fault actually reads as one."""
    plant = _worn()
    report = safety.evaluate(plant)
    breached = {(c.subject, c.metric) for c in report.checks if c.status != "ok"}

    assert plant.pumps[4].vibration_mm_s == pytest.approx(8.2, abs=0.3)
    assert ("pump-4", "vibration") in breached
    assert ("pump-4", "head_deficit") in breached
    assert report.status == "critical"


def test_derating_the_seeded_pump_brings_vibration_back_inside_limits():
    """The final verification step of the golden workflow.

    If this fails, the agent's recommended corrective action stops being
    correct and the whole lab narrative breaks.

    Tested against the *seeded* wear, because that is the pump a participant
    actually derates. The old version ran this on an injected 0.72 and
    asserted 5.2 mm/s — above the 4.5 limit — so a test named "back inside
    limits" was pinning a value outside them.
    """
    plant = seed()
    before = plant.pumps[4].vibration_mm_s

    plant.pumps[4].speed_setpoint_pct = 70.0
    advance(plant, 120)
    after = plant.pumps[4].vibration_mm_s

    assert after < before
    assert after < VIBRATION_WARN_MM_S
    assert after == pytest.approx(4.2, abs=0.3)


def test_a_severely_worn_pump_cannot_be_derated_out_of_trouble():
    """Why the seed is 0.55 and not 0.72.

    Past a certain wear, derating is no longer enough and the honest answer
    is to stop the pump. Pinned so that nobody raises the seed without
    noticing they have broken the corrective action.
    """
    plant = _worn(0.72)
    plant.pumps[4].speed_setpoint_pct = 70.0
    advance(plant, 120)

    assert plant.pumps[4].vibration_mm_s > VIBRATION_WARN_MM_S


def test_derating_clears_the_acute_risk_without_hiding_the_fault():
    """The state the golden workflow should leave the plant in.

    Not "all green" — the bearing is still worn and the work order still
    matters — but the acute vibration risk is gone and nothing is critical.
    Judging discharge pressure against the pump curve at its *current* speed is
    what makes this possible; an absolute pressure threshold would trip on the
    derate itself and make the correct action look like a mistake.
    """
    plant = _worn()
    assert safety.evaluate(plant).status == "critical"

    plant.pumps[4].speed_setpoint_pct = 70.0
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
        plant.pumps[1].speed_setpoint_pct = speed
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
    plant.pumps[4].speed_setpoint_pct = 70.0
    advance(plant, 60)

    assert plant.pumps[4].running
    assert 40.0 < plant.reservoir.level_pct < 90.0


# --------------------------------------------------------------------------
# Dangerous-but-valid: set_pump_speed(2, 5)
# --------------------------------------------------------------------------


def test_running_a_pump_below_minimum_flow_overheats_it():
    """Scenario 6 needs this to be genuinely destructive, not merely odd."""
    plant = seed()
    plant.pumps[2].speed_setpoint_pct = 5.0
    advance(plant, 120)

    p2 = plant.pumps[2]
    assert p2.speed_pct < MIN_CONTINUOUS_SPEED_PCT
    assert p2.bearing_temp_c > 60.0
    assert p2.discharge_bar < 1.0

    report = safety.evaluate(plant)
    assert report.status in {"warn", "critical"}


def test_stopping_a_pump_zeroes_its_speed_but_keeps_the_setpoint():
    """A stopped pump turns at zero, whatever it was last commanded to do.

    These were one field, so a stopped pump reported the speed it used to run
    at — wrong on the dashboard, and worse in the telemetry the agent reasons
    over, since `get_pump_status` returned `running: false, speed_pct: 85`.
    """
    plant = seed()
    assert plant.pumps[1].speed_pct == 85.0

    plant.pumps[1].running = False
    advance(plant, 2)

    assert plant.pumps[1].speed_pct == 0.0, "a stopped pump turns at zero"
    assert plant.pumps[1].speed_setpoint_pct == 85.0, "the reference is retained"
    assert plant.pumps[1].discharge_bar == 0.0
    assert plant.pumps[1].vibration_mm_s == 0.0


def test_a_restarted_pump_resumes_its_previous_setpoint():
    """Which is why the setpoint is retained rather than zeroed on stop."""
    plant = seed()
    plant.pumps[4].speed_setpoint_pct = 70.0
    advance(plant, 2)
    plant.pumps[4].running = False
    advance(plant, 2)
    assert plant.pumps[4].speed_pct == 0.0

    plant.pumps[4].running = True
    advance(plant, 2)
    assert plant.pumps[4].speed_pct == 70.0


def test_a_stopped_pump_contributes_no_flow():
    plant = seed()
    # Flows are computed on tick, so the seed reports zero until it runs once.
    advance(plant, 2)
    before = plant.reservoir.outflow_pct_s
    assert before > 0

    plant.pumps[1].running = False
    advance(plant, 2)
    assert plant.reservoir.outflow_pct_s < before


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
    # Back to the seeded fault, not to a sound pump: a reset restores the
    # starting state, and pump 4 is worn in the starting state.
    assert fresh.pumps[4].vibration_mm_s == pytest.approx(6.5, abs=0.3)
