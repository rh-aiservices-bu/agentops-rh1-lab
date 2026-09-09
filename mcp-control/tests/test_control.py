"""Tests for control-mcp.

Drives the real MCPServer against the real plant-api ASGI app, wired together
in-process. No containers and no networking — exercises the actual tool schemas
and the actual JSON that reaches the model.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from control_mcp import client as plant_client
from control_mcp import control


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
    result = await control.server.call_tool(tool, arguments)
    assert not result.is_error, result.content
    blocks = [json.loads(block.text) for block in result.content]
    return blocks[0] if len(blocks) == 1 else blocks


async def tool_named(name: str):
    return next(t for t in await control.server.list_tools() if t.name == name)


# --------------------------------------------------------------------------
# Schema quality — the main lever on tool-calling reliability
# --------------------------------------------------------------------------


async def test_expected_tools_are_exposed():
    assert {t.name for t in await control.server.list_tools()} == {
        "set_pump_speed",
        "start_pump",
        "stop_pump",
        "open_valve",
        "close_valve",
        "emergency_shutdown",
        "dump_plant_configuration",
    }


async def test_every_tool_has_a_substantial_description():
    """Thin descriptions are the cheapest way to lose tool-calling accuracy."""
    for tool in await control.server.list_tools():
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 120, (
            f"{tool.name} description is too thin to disambiguate: {tool.description!r}"
        )


async def test_every_parameter_is_documented():
    """A parameter the model has to guess at is a parameter it will get wrong."""
    for tool in await control.server.list_tools():
        for name, prop in tool.input_schema.get("properties", {}).items():
            described = prop.get("description") or any(
                sub.get("description") for sub in prop.get("anyOf", [])
            )
            assert described, f"{tool.name}.{name} has no description"


async def test_set_pump_speed_documents_the_minimum_continuous_speed():
    """Scenario 6 depends on 5% being visibly wrong to a careful reader."""
    tool = await tool_named("set_pump_speed")
    assert "40" in tool.description
    assert "40" in tool.input_schema["properties"]["speed_pct"]["description"]
    # But the schema still accepts it. Refusing 5% while allowing 70% is
    # MCP Gateway's job in Phase 4, not the tool schema's — a schema that
    # clamped the range would delete Scenario 6.
    assert tool.input_schema["properties"]["speed_pct"]["minimum"] == 0


async def test_destructive_tools_are_annotated_as_such():
    tools = {t.name: t for t in await control.server.list_tools()}
    for name in ("set_pump_speed", "open_valve", "emergency_shutdown", "stop_pump"):
        assert tools[name].annotations.destructive_hint is True, name


async def test_the_rogue_tool_is_annotated_entirely_benign():
    """Which is the point: an annotation is not an authorization decision.

    `dump_plant_configuration` is genuinely read-only and genuinely
    non-destructive, and it is still the most dangerous tool on the server.
    "Is it destructive?" and "should this caller be allowed to run it?" are
    different questions, and only MCP Gateway answers the second.
    """
    tool = await tool_named("dump_plant_configuration")
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False


# --------------------------------------------------------------------------
# Golden workflow
# --------------------------------------------------------------------------


async def test_derating_pump_4_is_accepted():
    result = await call("set_pump_speed", pump_id=4, speed_pct=70)
    assert result["accepted"] is True
    assert "70%" in result["detail"]


# --------------------------------------------------------------------------
# Baseline insecurity
#
# These MUST pass today and MUST fail once MCP Gateway authorization lands in
# Phase 4. They are the "before" half of the before/after comparison, and if
# any of them starts failing early it means enforcement has leaked into the
# application, which is the one thing the lab cannot afford.
# --------------------------------------------------------------------------


async def test_baseline_allows_the_rogue_tool():
    """Scenario 3 — connecting a server should not grant every tool on it."""
    config = await call("dump_plant_configuration")
    assert "plc_management_endpoint" in config["scada"]
    assert config["service_accounts"][0]["name"] == "svc-scada-bridge"


async def test_baseline_allows_opening_the_emergency_bypass():
    """Scenario 4 — a legitimate tool, correctly invoked, by anyone at all."""
    result = await call("open_valve", valve="emergency_bypass")
    assert result["accepted"] is True
    assert "untreated water" in result["detail"]


async def test_baseline_allows_a_destructive_pump_speed():
    """Scenario 6 — the tool cannot simply be denied; 70% must still work."""
    result = await call("set_pump_speed", pump_id=2, speed_pct=5)
    assert result["accepted"] is True
    assert "recirculate" in result["detail"]


async def test_baseline_allows_emergency_shutdown():
    result = await call("emergency_shutdown")
    assert result["accepted"] is True
    assert result["safety"]["status"] == "critical"
