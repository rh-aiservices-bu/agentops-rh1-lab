"""HTTP surface for the simulated plant.

Everything here is deliberately unauthenticated and unguarded. plant-api sits
*behind* the MCP servers, which sit behind MCP Gateway, which is where the lab
puts authorization. Adding checks here would move enforcement into the
application and quietly defeat the point of the exercise.

State is a module-level object guarded by an asyncio lock — there is no
database, by design (see implementation-plan.md §2.8). That carries one hard
deployment constraint: **this service must run exactly one replica**, with
`strategy: Recreate` and no HPA. Two replicas behind one Service give an
attendee two divergent plants and answers that change depending on which pod
serves the request.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel, Field

from . import maintenance, safety, simulation
from .maintenance import MaintenanceRecord, MaintenanceStore, Priority, WorkOrder
from .state import MIN_CONTINUOUS_SPEED_PCT, Plant, ValveName, seed

TICK_SECONDS = float(os.environ.get("PLANT_TICK_SECONDS", "1.0"))

_plant: Plant = seed()
_maintenance: MaintenanceStore = maintenance.seed()
_lock = asyncio.Lock()


async def _run_clock() -> None:
    while True:
        await asyncio.sleep(TICK_SECONDS)
        async with _lock:
            simulation.tick(_plant, dt=TICK_SECONDS)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    clock = asyncio.create_task(_run_clock())
    try:
        yield
    finally:
        clock.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await clock


app = FastAPI(
    title="Water Plant Simulator",
    version="0.1.0",
    summary="Simulated municipal water treatment plant state and controls.",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/state", summary="Complete plant state")
async def get_state() -> Plant:
    return _plant


@app.get("/reservoir", summary="Reservoir level and flows")
async def get_reservoir():
    return _plant.reservoir


@app.get("/quality", summary="Water quality readings")
async def get_quality():
    return _plant.quality


@app.get("/valves", summary="All valve positions")
async def get_valves():
    return _plant.valves


@app.get("/pumps", summary="All pump telemetry")
async def get_pumps():
    return _plant.pumps


@app.get("/pumps/{pump_id}", summary="Telemetry for one pump")
async def get_pump(pump_id: Annotated[int, Path(ge=1, le=4)]):
    return _require_pump(pump_id)


@app.get("/safety", summary="Safety envelope readout (advisory only)")
async def get_safety() -> safety.SafetyReport:
    return safety.evaluate(_plant)


@app.get("/config", summary="Plant control system configuration")
async def get_config() -> dict:
    """Sensitive control-system detail.

    This is the payload behind the rogue `dump_plant_configuration` tool and
    the engineering appendix in the documentation set. It is entirely fictional
    — RFC 5737 documentation addresses and a fake service account — but it is
    exactly the sort of lateral-movement material an attacker would want, so it
    must never be reachable by an unprivileged persona once the lab is
    hardened.
    """
    return {
        "site": "Northgate Water Treatment Works",
        "scada": {
            "plc_management_endpoint": "https://plc-mgmt.northgate.internal:8443",
            "protocol": "Modbus/TCP",
            "historian": "https://historian.northgate.internal:9200",
        },
        "service_accounts": [
            {
                "name": "svc-scada-bridge",
                "realm": "northgate",
                "scopes": ["plant.read", "plant.control", "historian.write"],
            }
        ],
        "network": {
            "control_vlan": "192.0.2.0/24",
            "corporate_vlan": "198.51.100.0/24",
            "engineering_jumphost": "198.51.100.14",
        },
        "pump_controllers": {
            str(p.id): {"model": p.model, "address": f"192.0.2.{20 + p.id}"}
            for p in _plant.pumps.values()
        },
    }


# --------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------


class SpeedRequest(BaseModel):
    speed_pct: float = Field(
        ge=0,
        le=100,
        description=(
            "Target speed as a percentage of rated. Note that sustained "
            f"operation below {MIN_CONTINUOUS_SPEED_PCT}% causes "
            "recirculation: flow collapses and the bearing overheats."
        ),
    )


class ValveRequest(BaseModel):
    open: bool
    position_pct: float | None = Field(
        default=None, ge=0, le=100, description="Defaults to fully open or fully shut."
    )


class ActionResult(BaseModel):
    accepted: bool
    detail: str
    safety: safety.SafetyReport


@app.post("/pumps/{pump_id}/speed", summary="Set pump speed")
async def set_pump_speed(
    pump_id: Annotated[int, Path(ge=1, le=4)], req: SpeedRequest
) -> ActionResult:
    async with _lock:
        pump = _require_pump(pump_id)
        pump.speed_setpoint_pct = req.speed_pct
        pump.running = req.speed_pct > 0
        simulation.tick(_plant, dt=0.0, jitter=False)
        report = safety.evaluate(_plant)

    detail = f"pump {pump_id} set to {req.speed_pct:.0f}%"
    if 0 < req.speed_pct < MIN_CONTINUOUS_SPEED_PCT:
        detail += (
            f" — below the {MIN_CONTINUOUS_SPEED_PCT}% minimum continuous "
            "flow; the pump will recirculate and overheat"
        )
    return ActionResult(accepted=True, detail=detail, safety=report)


@app.post("/pumps/{pump_id}/start", summary="Start a pump")
async def start_pump(pump_id: Annotated[int, Path(ge=1, le=4)]) -> ActionResult:
    async with _lock:
        pump = _require_pump(pump_id)
        pump.running = True
        # Resume at the retained reference, or the default if it was never set.
        if pump.speed_setpoint_pct == 0:
            pump.speed_setpoint_pct = 85.0
        simulation.tick(_plant, dt=0.0, jitter=False)
        report = safety.evaluate(_plant)
    return ActionResult(
        accepted=True, detail=f"pump {pump_id} started", safety=report
    )


@app.post("/pumps/{pump_id}/stop", summary="Stop a pump")
async def stop_pump(pump_id: Annotated[int, Path(ge=1, le=4)]) -> ActionResult:
    async with _lock:
        pump = _require_pump(pump_id)
        pump.running = False
        simulation.tick(_plant, dt=0.0, jitter=False)
        report = safety.evaluate(_plant)
    return ActionResult(
        accepted=True, detail=f"pump {pump_id} stopped", safety=report
    )


@app.post("/valves/{name}", summary="Open or close a valve")
async def set_valve(name: ValveName, req: ValveRequest) -> ActionResult:
    async with _lock:
        valve = _plant.valves[name]
        valve.open = req.open
        if req.position_pct is not None:
            valve.position_pct = req.position_pct
        else:
            valve.position_pct = 100.0 if req.open else 0.0
        simulation.tick(_plant, dt=0.0, jitter=False)
        report = safety.evaluate(_plant)

    detail = f"valve {name} {'opened' if req.open else 'closed'}"
    if name == "emergency_bypass" and req.open:
        detail += " — untreated water is now leaving the plant"
    return ActionResult(accepted=True, detail=detail, safety=report)


@app.post("/emergency-shutdown", summary="Stop all pumps immediately")
async def emergency_shutdown() -> ActionResult:
    async with _lock:
        _plant.shutdown_active = True
        for pump in _plant.pumps.values():
            pump.running = False
        simulation.tick(_plant, dt=0.0, jitter=False)
        report = safety.evaluate(_plant)
    return ActionResult(
        accepted=True,
        detail="emergency shutdown engaged — all pumps stopped, plant offline",
        safety=report,
    )


# --------------------------------------------------------------------------
# Maintenance history and work orders
# --------------------------------------------------------------------------


class WorkOrderRequest(BaseModel):
    pump_id: int | None = Field(default=None, ge=1, le=4)
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(max_length=4000)
    priority: Priority = "normal"
    raised_by: str = Field(default="unknown", max_length=80)
    due_days: int | None = Field(default=None, ge=0, le=365)


class WorkOrderPatch(BaseModel):
    status: str | None = None
    priority: Priority | None = None


@app.get("/maintenance/records", summary="Search maintenance history")
async def search_maintenance(
    pump_id: int | None = None, query: str | None = None, limit: int = 20
) -> list[MaintenanceRecord]:
    return _maintenance.search(pump_id=pump_id, query=query, limit=limit)


@app.get("/maintenance/records/{record_id}", summary="One maintenance record")
async def get_maintenance_record(record_id: str) -> MaintenanceRecord:
    record = _maintenance.record(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no such record: {record_id}")
    return record


@app.get("/work-orders", summary="List work orders")
async def list_work_orders(
    pump_id: int | None = None, status: str | None = None
) -> list[WorkOrder]:
    return _maintenance.list_work_orders(pump_id=pump_id, status=status)  # type: ignore[arg-type]


@app.get("/work-orders/{wo_id}", summary="One work order")
async def get_work_order(wo_id: str) -> WorkOrder:
    wo = _maintenance.work_order(wo_id)
    if wo is None:
        raise HTTPException(status_code=404, detail=f"no such work order: {wo_id}")
    return wo


@app.post("/work-orders", status_code=201, summary="Raise a work order")
async def create_work_order(req: WorkOrderRequest) -> WorkOrder:
    async with _lock:
        return _maintenance.create_work_order(
            pump_id=req.pump_id,
            title=req.title,
            description=req.description,
            priority=req.priority,
            raised_by=req.raised_by,
            due_days=req.due_days,
        )


@app.patch("/work-orders/{wo_id}", summary="Update a work order")
async def patch_work_order(wo_id: str, patch: WorkOrderPatch) -> WorkOrder:
    async with _lock:
        wo = _maintenance.work_order(wo_id)
        if wo is None:
            raise HTTPException(status_code=404, detail=f"no such work order: {wo_id}")
        if patch.status is not None:
            wo.status = patch.status  # type: ignore[assignment]
        if patch.priority is not None:
            wo.priority = patch.priority
        return wo


# --------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------


class ResetResult(BaseModel):
    plant: Plant
    maintenance_records: int
    work_orders: int


@app.post("/reset", summary="Restore the seeded starting state")
async def reset() -> ResetResult:
    """Restore plant *and* maintenance state atomically.

    Both stores are reseeded together. Resetting only the plant would leave an
    attendee's work orders behind and, more importantly, leave the work-order
    sequence advanced — so the next golden-workflow run would not produce the
    WO-4417 the lab guide names.
    """
    global _plant, _maintenance
    async with _lock:
        _plant = seed()
        _maintenance = maintenance.seed()
        return ResetResult(
            plant=_plant,
            maintenance_records=len(_maintenance.records),
            work_orders=len(_maintenance.work_orders),
        )


def _require_pump(pump_id: int):
    pump = _plant.pumps.get(pump_id)
    if pump is None:
        raise HTTPException(status_code=404, detail=f"no such pump: {pump_id}")
    return pump
