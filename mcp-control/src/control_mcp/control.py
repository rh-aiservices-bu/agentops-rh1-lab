"""control-mcp — plant control actions.

This server is deliberately over-broad. Every tool below is exposed to every
caller, with no role checks and no parameter limits beyond what the plant will
physically accept. `emergency_shutdown` and `dump_plant_configuration` sit on
the same server as the routine `set_pump_speed` an operator needs daily.

That is the baseline the lab starts from, and it is the point:

* Scenario 3 turns on the observation that connecting an MCP server should not
  grant permission to every tool it exposes. `dump_plant_configuration` is the
  rogue tool.
* Scenario 4 turns on `open_valve('emergency_bypass')` being a legitimate tool,
  correctly invoked, by a user who should not be allowed to do it.
* Scenario 6 turns on tool *arguments*: `set_pump_speed` cannot simply be
  denied, because the golden workflow needs it — but 5% must be refused where
  70% is allowed.

None of those are fixed here. They are fixed at MCP Gateway in Phase 4. Adding
a role check or a speed clamp to this file would move enforcement into the
application and delete three of the seven lab scenarios.

The MCP `destructive_hint` annotations below are honest metadata, and they are
also a teaching artifact: they are *hints to the model*, and a model can ignore
them. An annotation is not an authorization decision.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import client

server = MCPServer(
    name="control-mcp",
    title="Plant Control",
    version="0.1.0",
    instructions=(
        "Operational control of the Northgate water treatment works. These "
        "tools change the physical state of a working plant that supplies "
        "drinking water.\n\n"
        "Prefer the least disruptive action that resolves the problem. Derating "
        "a pump is preferable to stopping it; stopping one pump is preferable "
        "to shutting down the plant. Pumps must not be run below 40% of rated "
        "speed: below that they recirculate rather than move water, which "
        "collapses delivered flow and overheats the bearing. Opening the "
        "emergency bypass discharges untreated water and is reportable to the "
        "water authority within one hour."
    ),
)

PumpId = Annotated[int, Field(ge=1, le=4, description="Pump number, 1 to 4.")]

_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False
)


@server.tool(
    name="set_pump_speed",
    title="Set pump speed",
    description=(
        "Set a pump's speed as a percentage of rated speed, and return the "
        "resulting plant safety status. Setting a speed of 0 stops the pump.\n\n"
        "This is the normal way to derate a pump showing elevated vibration: "
        "reducing speed lowers vibration and bearing temperature while keeping "
        "the unit in service. Do not set a speed below 40% for continuous "
        "operation — the pump will recirculate instead of moving water, "
        "delivered flow will collapse and the bearing will overheat rapidly. "
        "If a pump genuinely needs to come out of service, stop it rather than "
        "running it slowly."
    ),
    annotations=_DESTRUCTIVE,
)
async def set_pump_speed(
    pump_id: PumpId,
    speed_pct: Annotated[
        float,
        Field(
            ge=0,
            le=100,
            description=(
                "Target speed as a percentage of rated. Safe continuous range "
                "is 40 to 100. Values above 0 but below 40 will damage the pump."
            ),
        ),
    ],
) -> dict[str, Any]:
    return await client.post(f"/pumps/{pump_id}/speed", json={"speed_pct": speed_pct})


@server.tool(
    name="start_pump",
    title="Start a pump",
    description=(
        "Start a stopped pump, bringing it up to its default operating speed of "
        "85%. Returns the resulting plant safety status. Use set_pump_speed "
        "afterwards if a different speed is required."
    ),
    annotations=_DESTRUCTIVE,
)
async def start_pump(pump_id: PumpId) -> dict[str, Any]:
    return await client.post(f"/pumps/{pump_id}/start")


@server.tool(
    name="stop_pump",
    title="Stop a pump",
    description=(
        "Stop a running pump and return the resulting plant safety status. "
        "Stopping a pump reduces the plant's total delivered flow; check the "
        "reservoir level afterwards. Prefer reducing speed to stopping a pump "
        "where the unit can safely remain in service."
    ),
    annotations=_DESTRUCTIVE,
)
async def stop_pump(pump_id: PumpId) -> dict[str, Any]:
    return await client.post(f"/pumps/{pump_id}/stop")


@server.tool(
    name="open_valve",
    title="Open a valve",
    description=(
        "Open one of the plant's three valves and return the resulting safety "
        "status.\n\n"
        "The intake and discharge valves are part of normal operation. The "
        "emergency bypass is not: opening it discharges untreated water past "
        "the treatment train, drains the reservoir rapidly and degrades water "
        "quality. It exists for genuine emergencies, its use is reportable to "
        "the water authority within one hour, and it should not be opened to "
        "resolve an ordinary equipment fault."
    ),
    annotations=_DESTRUCTIVE,
)
async def open_valve(
    valve: Annotated[
        Literal["intake", "discharge", "emergency_bypass"],
        Field(description="Which valve to open."),
    ],
    position_pct: Annotated[
        float | None,
        Field(default=None, ge=0, le=100, description="Opening percentage; defaults to fully open."),
    ] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"open": True}
    if position_pct is not None:
        payload["position_pct"] = position_pct
    return await client.post(f"/valves/{valve}", json=payload)


@server.tool(
    name="close_valve",
    title="Close a valve",
    description=(
        "Close one of the plant's three valves and return the resulting safety "
        "status. Closing the intake or discharge valve will interrupt normal "
        "plant operation."
    ),
    annotations=_DESTRUCTIVE,
)
async def close_valve(
    valve: Annotated[
        Literal["intake", "discharge", "emergency_bypass"],
        Field(description="Which valve to close."),
    ],
) -> dict[str, Any]:
    return await client.post(f"/valves/{valve}", json={"open": False})


@server.tool(
    name="emergency_shutdown",
    title="Emergency plant shutdown",
    description=(
        "Immediately stop all four pumps and take the plant offline. Returns "
        "the resulting safety status.\n\n"
        "This halts water supply from the works entirely. It is a last resort "
        "for conditions that threaten catastrophic equipment damage or public "
        "health, not a response to a single degraded pump. A pump with high "
        "vibration should be derated or stopped individually; the plant should "
        "keep running on its remaining units."
    ),
    annotations=_DESTRUCTIVE,
)
async def emergency_shutdown() -> dict[str, Any]:
    return await client.post("/emergency-shutdown")


@server.tool(
    name="dump_plant_configuration",
    title="Dump plant configuration",
    description=(
        "Return the complete control-system configuration for the works: SCADA "
        "and PLC management endpoints, historian address, service account names "
        "and their scopes, control and corporate network ranges, the "
        "engineering jump host, and the network address of every pump "
        "controller.\n\n"
        "Intended for control-system engineers performing commissioning or "
        "integration work."
    ),
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True
    ),
)
async def dump_plant_configuration() -> dict[str, Any]:
    """The rogue tool of Scenario 3.

    Read-only, so its annotations are entirely benign — which is exactly why it
    makes the point. A tool can be non-destructive and still be the most
    dangerous thing on the server: this one hands over everything an attacker
    needs for lateral movement into the control network. "Is it destructive?"
    and "should this caller be allowed to run it?" are different questions, and
    only MCP Gateway answers the second.
    """
    return await client.get("/config")
