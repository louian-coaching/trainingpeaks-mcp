"""FORK 2026-10-03: the default 'coach' toolset hides unused upstream tools."""

import json

import pytest

from tp_mcp import server


@pytest.mark.asyncio
async def test_coach_toolset_hides_and_refuses(monkeypatch):
    monkeypatch.setenv("TP_MCP_TOOLSET", "coach")
    names = {t.name for t in await server.list_tools()}
    assert "tp_update_workout" not in names and "tp_get_equipment" not in names
    assert {"lo_update_workout_verified", "tp_create_workouts_batch", "tp_get_workouts",
            "lo_update_threshold_verified", "tp_get_availability", "tp_create_note"} <= names
    out = json.loads((await server.call_tool("tp_update_workout", {"workout_id": "1"}))[0].text)
    assert out["error_code"] == "HIDDEN_TOOL" and "lo_update_workout_verified" in out["message"]


@pytest.mark.asyncio
async def test_full_toolset_is_upstream_surface(monkeypatch):
    monkeypatch.setenv("TP_MCP_TOOLSET", "full")
    assert await server.list_tools() is server.TOOLS


def test_hidden_names_all_exist():
    names = {t.name for t in server.TOOLS}
    assert set(server._COACH_HIDDEN) <= names


@pytest.mark.asyncio
async def test_coach_toolset_is_meaningfully_smaller(monkeypatch):
    monkeypatch.setenv("TP_MCP_TOOLSET", "coach")
    full = sum(len(json.dumps(t.model_dump(), ensure_ascii=False)) for t in server.TOOLS)
    coach = sum(len(json.dumps(t.model_dump(), ensure_ascii=False)) for t in await server.list_tools())
    assert coach < full * 0.8


@pytest.mark.asyncio
async def test_coach_hides_comment_and_raw_threshold_writes(monkeypatch):
    """FORK 2026-11-06: comments forbidden (CLAUDE.md); thresholds only via verified wrapper."""
    monkeypatch.setenv("TP_MCP_TOOLSET", "coach")
    names = {t.name for t in await server.list_tools()}
    for n in ("tp_add_workout_comment", "tp_update_ftp", "tp_update_hr_zones", "tp_update_speed_zones"):
        assert n not in names
    out = json.loads((await server.call_tool("tp_update_ftp", {"ftp": 250}))[0].text)
    assert out["error_code"] == "HIDDEN_TOOL" and "lo_update_threshold_verified" in out["message"]
    out = json.loads((await server.call_tool("tp_add_workout_comment", {"workout_id": "1", "comment": "x"}))[0].text)
    assert out["error_code"] == "HIDDEN_TOOL" and "description" in out["message"]
