"""Tests for telemetry-mcp.

Drives the real MCPServer against the real plant-api ASGI app, wired together
in-process. No containers and no networking — exercises the actual tool schemas
and the actual JSON that reaches the model.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from telemetry_mcp import client as plant_client
from telemetry_mcp import telemetry


@pytest.fixture(autouse=True)
def wire_to_plant_api():
    """Point the MCP server's HTTP client at an in-process plant-api."""
    from plant_api import api as plant_api_module
    from plant_api import maintenance as plant_maintenance
    from plant_api.api import app as plant_app
    from plant_api.state import seed as plant_seed

    plant_api_module._plant = plant_seed()
    plant_api_module._maintenance = plant_maintenance.seed()

    plant_client._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=plant_app), base_url="http://plant-api"
    )
    yield
    plant_client._client = None


async def call(tool: str, **arguments):
    result = await telemetry.server.call_tool(tool, arguments)
    assert not result.is_error, result.content
    blocks = [json.loads(block.text) for block in result.content]
    return blocks[0] if len(blocks) == 1 else blocks


async def tool_named(name: str):
    return next(t for t in await telemetry.server.list_tools() if t.name == name)


# --------------------------------------------------------------------------
# Schema quality — the main lever on tool-calling reliability
# --------------------------------------------------------------------------


async def test_expected_tools_are_exposed():
    assert {t.name for t in await telemetry.server.list_tools()} == {
        "get_plant_safety_status",
        "get_pump_status",
        "get_all_pump_status",
        "get_water_quality",
        "get_reservoir_level",
        "get_valve_positions",
    }


async def test_every_tool_has_a_substantial_description():
    """Thin descriptions are the cheapest way to lose tool-calling accuracy."""
    for tool in await telemetry.server.list_tools():
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 120, (
            f"{tool.name} description is too thin to disambiguate: {tool.description!r}"
        )


async def test_every_parameter_is_documented():
    """A parameter the model has to guess at is a parameter it will get wrong."""
    for tool in await telemetry.server.list_tools():
        for name, prop in tool.input_schema.get("properties", {}).items():
            described = prop.get("description") or any(
                sub.get("description") for sub in prop.get("anyOf", [])
            )
            assert described, f"{tool.name}.{name} has no description"


async def test_pump_id_is_constrained_in_the_schema():
    """Constraints belong in the schema, not in prose the model may ignore."""
    tool = await tool_named("get_pump_status")
    prop = tool.input_schema["properties"]["pump_id"]
    assert prop["minimum"] == 1
    assert prop["maximum"] == 4


# --------------------------------------------------------------------------
# Golden workflow
# --------------------------------------------------------------------------


async def test_telemetry_surfaces_the_pump_4_fault():
    status = await call("get_plant_safety_status")
    assert status["status"] == "critical"

    pump = await call("get_pump_status", pump_id=4)
    assert pump["vibration_mm_s"] > 7.0
    assert pump["speed_pct"] == 100.0


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


async def test_out_of_range_pump_is_rejected_by_the_schema():
    with pytest.raises(ToolError) as exc:
        await telemetry.server.call_tool("get_pump_status", {"pump_id": 9})
    assert "less than or equal to 4" in str(exc.value)


async def test_plant_api_unreachable_is_reported_clearly():
    plant_client._client = httpx.AsyncClient(base_url="http://127.0.0.1:9")
    with pytest.raises(ToolError) as exc:
        await telemetry.server.call_tool("get_water_quality", {})
    assert "unreachable" in str(exc.value)
