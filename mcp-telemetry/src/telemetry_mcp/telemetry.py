"""telemetry-mcp — read-only plant instrumentation.

Every tool here is read-only and safe to call speculatively. Tool descriptions
are written to do two jobs: say precisely what the tool returns, and steer the
model toward the right *sequence*. A seven-step tool chain at 95% per-step
reliability completes 70% of the time, and the cheapest place to buy reliability
back is unambiguous schemas.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import client

server = MCPServer(
    name="telemetry-mcp",
    title="Plant Telemetry",
    version="0.1.0",
    instructions=(
        "Live instrumentation for the Northgate water treatment works. Use these "
        "tools to read current conditions before drawing any conclusion about "
        "plant behaviour. They are read-only and safe to call at any time. "
        "When investigating a fault, start with get_plant_safety_status to see "
        "which readings are outside limits, then use get_pump_status for detail "
        "on the specific unit involved."
    ),
)

PumpId = Annotated[
    int,
    Field(
        ge=1,
        le=4,
        description="Pump number. The plant has four pumps, numbered 1 to 4.",
    ),
]

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


@server.tool(
    name="get_plant_safety_status",
    title="Plant safety status",
    description=(
        "Report which plant readings are currently outside their safe operating "
        "limits, across all pumps, the reservoir and water quality. Returns an "
        "overall status of 'ok', 'warn' or 'critical' plus a per-reading "
        "breakdown showing the measured value and the limit it is judged "
        "against.\n\n"
        "This is the fastest way to find out what is wrong with the plant, so "
        "prefer it as the first call when investigating a reported problem or "
        "when asked for a general health check. Note that it reports "
        "conditions only — it does not take or recommend any action."
    ),
    annotations=_READ_ONLY,
)
async def get_plant_safety_status() -> dict[str, Any]:
    return await client.get("/safety")


@server.tool(
    name="get_pump_status",
    title="Pump telemetry",
    description=(
        "Read live telemetry for one pump: whether it is running, its actual "
        "speed as a percentage of rated, the commanded speed setpoint, "
        "discharge pressure in bar, bearing "
        "temperature in degrees Celsius, vibration in mm/s RMS, accumulated "
        "duty hours, and a wear estimate from 0.0 (new) to 1.0 (failed).\n\n"
        "`speed_pct` is what the pump is actually turning at and reads zero "
        "whenever it is stopped. `speed_setpoint_pct` is the retained speed "
        "reference the drive resumes from when restarted, so a stopped pump "
        "still shows the setpoint it will come back at. Report the actual "
        "speed unless asked what the pump is set to.\n\n"
        "Vibration is the most useful single indicator of mechanical condition: "
        "sustained readings above 4.5 mm/s indicate a developing fault and "
        "above 7.0 mm/s require intervention. Judge discharge pressure against "
        "the pump curve for the *current* speed rather than an absolute number, "
        "because a pump running slower is expected to produce less pressure — "
        "get_plant_safety_status does this comparison for you and reports it as "
        "a head deficit percentage."
    ),
    annotations=_READ_ONLY,
)
async def get_pump_status(pump_id: PumpId) -> dict[str, Any]:
    return await client.get(f"/pumps/{pump_id}")


@server.tool(
    name="get_all_pump_status",
    title="All pump telemetry",
    description=(
        "Read live telemetry for all four pumps at once, in the same form as "
        "get_pump_status. Use this when comparing pumps or when you do not yet "
        "know which unit is involved; use get_pump_status when you already know "
        "the pump number, to keep the response small."
    ),
    annotations=_READ_ONLY,
)
async def get_all_pump_status() -> dict[str, Any]:
    return await client.get("/pumps")


@server.tool(
    name="get_water_quality",
    title="Water quality",
    description=(
        "Read current treated water quality: pH, residual chlorine in mg/L, and "
        "turbidity in NTU. Normal operation is pH 6.5-8.5, chlorine 0.5-2.0 "
        "mg/L, and turbidity below 1.0 NTU. Turbidity rising above 1.0 NTU "
        "indicates water is leaving the plant inadequately treated, which is a "
        "reportable condition."
    ),
    annotations=_READ_ONLY,
)
async def get_water_quality() -> dict[str, Any]:
    return await client.get("/quality")


@server.tool(
    name="get_reservoir_level",
    title="Reservoir level and flows",
    description=(
        "Read the reservoir level as a percentage of capacity, along with "
        "current inflow and outflow rates in percent per second and the total "
        "capacity in cubic metres. The intake is level-controlled and normally "
        "holds the reservoir near 62%. A level below 20% or above 95% is a "
        "critical condition."
    ),
    annotations=_READ_ONLY,
)
async def get_reservoir_level() -> dict[str, Any]:
    return await client.get("/reservoir")


@server.tool(
    name="get_valve_positions",
    title="Valve positions",
    description=(
        "Read the state of all three plant valves — intake, discharge and "
        "emergency bypass — showing whether each is open and its position as a "
        "percentage. The emergency bypass should normally be closed: when open, "
        "untreated water leaves the plant."
    ),
    annotations=_READ_ONLY,
)
async def get_valve_positions() -> dict[str, Any]:
    return await client.get("/valves")
