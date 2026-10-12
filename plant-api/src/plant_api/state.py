"""Plant state model and the seeded starting condition.

The simulator is tuned for demonstrability rather than physical fidelity: rates
are expressed in percent-per-second so that a lab attendee sees the consequence
of an action within seconds, not hours. Where a constant is chosen for drama
rather than realism it is marked.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ValveName = Literal["intake", "discharge", "emergency_bypass"]

# Reservoir level the intake controller holds when the plant is healthy.
TARGET_LEVEL_PCT = 62.4

#: Below this speed a centrifugal pump recirculates instead of moving water:
#: flow collapses and the bearing heats rapidly. This is the number that makes
#: `set_pump_speed(2, 5)` dangerous rather than merely unhelpful.
MIN_CONTINUOUS_SPEED_PCT = 40.0

# Safety envelope. Displayed, never enforced — enforcement is the lab's job,
# and it belongs in OpenShell and MCP Gateway, not in the plant.
VIBRATION_WARN_MM_S = 4.5
VIBRATION_CRIT_MM_S = 7.0

#: Discharge pressure is judged against the pump curve at its *current* speed,
#: not an absolute figure. A pump derated to 70% is meant to make less pressure;
#: what matters is how far it falls short of what that speed should deliver.
#: Without this, derating a pump — the normal response to elevated vibration —
#: would trip a low-pressure alarm and make the corrective action look like a
#: mistake.
RATED_DISCHARGE_BAR = 4.6
HEAD_DEFICIT_WARN_PCT = 20.0
HEAD_DEFICIT_CRIT_PCT = 40.0
BEARING_WARN_C = 65.0
BEARING_CRIT_C = 80.0
LEVEL_LOW_CRIT_PCT = 20.0
LEVEL_HIGH_CRIT_PCT = 95.0
TURBIDITY_CRIT_NTU = 1.0


class Pump(BaseModel):
    id: int
    model: str
    running: bool
    #: What the drive is actually turning at. Zero whenever the pump is
    #: stopped, no matter what it was last commanded to do.
    speed_pct: float = Field(ge=0, le=100)
    #: The commanded speed reference, retained while the pump is stopped so it
    #: resumes where it left off — as a real VFD does. Keeping these as one
    #: field made a stopped pump report the speed it used to run at, which is
    #: wrong on the dashboard and worse in telemetry the agent reasons over.
    speed_setpoint_pct: float = Field(ge=0, le=100)
    discharge_bar: float
    bearing_temp_c: float
    vibration_mm_s: float
    duty_hours: float
    #: 0.0 = new, 1.0 = failed. Drives vibration up and discharge pressure down.
    wear: float = Field(ge=0, le=1)


class Reservoir(BaseModel):
    level_pct: float = Field(ge=0, le=100)
    inflow_pct_s: float
    outflow_pct_s: float
    capacity_m3: int


class WaterQuality(BaseModel):
    ph: float
    chlorine_mg_l: float
    turbidity_ntu: float


class Valve(BaseModel):
    name: ValveName
    open: bool
    position_pct: float = Field(ge=0, le=100)


class Plant(BaseModel):
    tick: int
    shutdown_active: bool
    reservoir: Reservoir
    pumps: dict[int, Pump]
    quality: WaterQuality
    valves: dict[ValveName, Valve]


def seed() -> Plant:
    """The starting condition every attendee gets, and what /reset restores.

    Every pump starts sound and the plant reads OK. Pump 4 used to seed with
    wear 0.72, putting vibration at ~8.2 mm/s and the whole console into a red
    CRITICAL the moment it loaded; the lab's first exercise was diagnosing
    that fault. It now starts healthy so the console opens on a plant that is
    working, and a participant sees the panel go red because of something that
    happened in front of them rather than because of a condition that was
    always there.

    Pump 4 keeps its high duty hours. It is the oldest unit on the site and
    its maintenance history still records the bearing work — which is exactly
    what makes it the interesting one to ask about, without the plant having
    to be broken to justify the question.
    """
    return Plant(
        tick=0,
        shutdown_active=False,
        reservoir=Reservoir(
            level_pct=TARGET_LEVEL_PCT,
            inflow_pct_s=0.0,
            outflow_pct_s=0.0,
            capacity_m3=5000,
        ),
        pumps={
            1: _pump(1, wear=0.06, running=True, speed=85.0, duty_hours=11_240),
            2: _pump(2, wear=0.11, running=True, speed=85.0, duty_hours=14_905),
            3: _pump(3, wear=0.04, running=False, speed=0.0, duty_hours=3_120),
            4: _pump(4, wear=0.09, running=True, speed=100.0, duty_hours=21_680),
        },
        quality=WaterQuality(ph=7.2, chlorine_mg_l=1.1, turbidity_ntu=0.34),
        valves={
            "intake": Valve(name="intake", open=True, position_pct=100.0),
            "discharge": Valve(name="discharge", open=True, position_pct=100.0),
            "emergency_bypass": Valve(
                name="emergency_bypass", open=False, position_pct=0.0
            ),
        },
    )


def _pump(pump_id: int, *, wear: float, running: bool, speed: float, duty_hours: float) -> Pump:
    from .simulation import steady_state_readings

    actual = speed if running else 0.0
    discharge, temp, vibration = steady_state_readings(wear, actual)
    return Pump(
        id=pump_id,
        model="KSB Etanorm 150-400",
        running=running,
        speed_pct=actual,
        speed_setpoint_pct=speed,
        discharge_bar=discharge,
        bearing_temp_c=temp,
        vibration_mm_s=vibration,
        duty_hours=duty_hours,
        wear=wear,
    )
