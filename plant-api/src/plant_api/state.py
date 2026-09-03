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
#: Without this, correctly derating Pump 4 would trip a low-pressure alarm and
#: the golden workflow's corrective action would look like a mistake.
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
    speed_pct: float = Field(ge=0, le=100)
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

    Pump 4 carries a degrading bearing: wear high enough that vibration sits
    well above the 4.5 mm/s limit and discharge pressure has fallen to ~3.1 bar.
    This is the fault the golden workflow diagnoses, and derating the pump to
    70% brings vibration back inside limits without stopping it — which is what
    makes "investigate and take the permitted corrective action" a real task
    rather than a scripted one.
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
            4: _pump(4, wear=0.72, running=True, speed=100.0, duty_hours=21_680),
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

    discharge, temp, vibration = steady_state_readings(wear, speed if running else 0.0)
    return Pump(
        id=pump_id,
        model="KSB Etanorm 150-400",
        running=running,
        speed_pct=speed,
        discharge_bar=discharge,
        bearing_temp_c=temp,
        vibration_mm_s=vibration,
        duty_hours=duty_hours,
        wear=wear,
    )
