"""Tests for tools/lo_review.py — completion is measured per method/0 原則五."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.tools.lo_review import lo_get_workouts_summary, summarize_workouts


def _w(**over):
    w = {
        "id": "1", "date": "2026-09-17", "title": "節奏騎", "type": "completed",
        "sport": "Bike", "duration_planned": None, "duration_actual": None,
        "distance_planned_km": None, "distance_actual_km": None,
        "tss_planned": None, "tss_actual": None,
        "description": "- 熱身騎10分鐘\n－－\n" + ("長篇說明 " * 200),
    }
    w.update(over)
    return w


# ---------------------------------------------------------------------------
# The basis table (method/5 §5.0 排課前置①)
# ---------------------------------------------------------------------------


def test_bike_completion_is_time_not_tss():
    """The 2026/09/17 case: TSS says 57%, but the honest number is the clock."""
    out = summarize_workouts([_w(
        duration_planned=83 / 60, duration_actual=52.7 / 60,
        tss_planned=93.1, tss_actual=52.75,
    )])
    row = out["workouts"][0]
    assert row["basis"] == "duration"
    assert (row["planned"], row["actual"], row["unit"]) == (83.0, 52.7, "min")
    assert row["done_pct"] == 63.5          # time, not the 56.7% TSS ratio
    assert row["tss_planned"] == 93.1       # raw figures still travel
    assert "tss_pct" not in row and "compliance_pct" not in row


def test_swim_completion_is_metres():
    """巴斯 2026/08/03: TSS said 74%, the athlete swam 103% of the metres."""
    out = summarize_workouts([_w(
        sport="Swim", title="閾值間歇",
        distance_planned_km=3.8, distance_actual_km=3.9,
        duration_planned=1.2, duration_actual=1.3,
        tss_planned=76, tss_actual=56.2,
    )])
    row = out["workouts"][0]
    assert row["basis"] == "distance"
    assert (row["planned"], row["actual"], row["unit"]) == (3800, 3900, "m")
    assert row["done_pct"] == 102.6
    assert "incomplete" not in out          # 做滿還超做，不該被點名


def test_run_with_prescribed_distance_uses_distance():
    out = summarize_workouts([_w(
        sport="Run", title="長跑", distance_planned_km=25.0, distance_actual_km=25.7,
        duration_planned=2.07, duration_actual=2.05,
    )])
    row = out["workouts"][0]
    assert row["basis"] == "distance"
    assert row["unit"] == "km"
    assert row["done_pct"] == 102.8


def test_time_based_run_falls_back_to_time():
    """輕鬆跑 carries no prescribed distance — judge it on the clock."""
    out = summarize_workouts([_w(
        sport="Run", title="輕鬆跑", duration_planned=40 / 60, duration_actual=38 / 60,
    )])
    row = out["workouts"][0]
    assert row["basis"] == "duration"
    assert row["done_pct"] == 95.0


def test_swim_without_prescribed_distance_falls_back_not_zero():
    out = summarize_workouts([_w(
        sport="Swim", duration_planned=1.0, duration_actual=1.0, distance_actual_km=2.5,
    )])
    row = out["workouts"][0]
    assert row["basis"] == "duration"
    assert row["done_pct"] == 100.0


def test_strength_uses_time():
    out = summarize_workouts([_w(
        sport="Strength", title="力量訓練",
        duration_planned=1.0, duration_actual=0.9, tss_planned=30,
    )])
    assert out["workouts"][0]["basis"] == "duration"
    assert out["workouts"][0]["done_pct"] == 90.0


# ---------------------------------------------------------------------------
# What the reviewer actually reads
# ---------------------------------------------------------------------------


def test_descriptions_are_dropped():
    out = summarize_workouts([_w(duration_planned=1.0, duration_actual=1.0)])
    assert "description" not in out["workouts"][0]


def test_incomplete_names_the_sessions():
    out = summarize_workouts([
        _w(id="1", title="節奏騎", duration_planned=83 / 60, duration_actual=52.7 / 60),
        _w(id="2", title="長距離騎乘", duration_planned=3.67, duration_actual=3.64),
    ])
    assert [i["title"] for i in out["incomplete"]] == ["節奏騎"]
    assert out["incomplete"][0]["done_pct"] == 63.5


def test_incomplete_threshold_is_tunable():
    rows = [_w(duration_planned=1.0, duration_actual=0.95)]
    assert "incomplete" not in summarize_workouts(rows, incomplete_below=90)
    assert len(summarize_workouts(rows, incomplete_below=97)["incomplete"]) == 1


def test_planned_only_workouts_are_not_flagged_incomplete():
    """A future session has no actuals — it is not a failure to complete."""
    out = summarize_workouts([_w(type="planned", duration_planned=1.0)])
    assert "incomplete" not in out
    assert out["workouts"][0].get("done_pct") is None


def test_unplanned_sessions_surface():
    """The athlete added a swim nobody prescribed (forcetop 9/12)."""
    out = summarize_workouts([_w(
        sport="Swim", title="Lap Swimming", distance_actual_km=2.0,
        duration_actual=1.0, tss_actual=69.5,
    )])
    assert out["unplanned"][0]["title"] == "Lap Swimming"
    assert "incomplete" not in out


def test_totals_keep_tss_as_figures_and_split_volume_by_sport():
    out = summarize_workouts([
        _w(sport="Bike", duration_planned=1.0, duration_actual=1.0,
           tss_planned=50, tss_actual=48),
        _w(sport="Swim", distance_planned_km=3.0, distance_actual_km=3.0,
           tss_planned=68, tss_actual=69.5),
    ])
    assert out["totals"]["tss_planned"] == 118.0
    assert out["totals"]["tss_actual"] == 117.5
    assert out["totals"]["per_sport"]["Bike"]["planned_min"] == 60.0
    assert out["totals"]["per_sport"]["Swim"]["actual_m"] == 3000.0


def test_note_points_at_the_rule():
    out = summarize_workouts([])
    assert "原則五" in out["note"] and "不得用來判斷完成度" in out["note"]


# ---------------------------------------------------------------------------
# Tool wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_passes_filter_and_keeps_date_range():
    listed = {
        "workouts": [_w(duration_planned=1.0, duration_actual=1.0)],
        "count": 1,
        "date_range": {"start": "2026-09-14", "end": "2026-09-20"},
    }
    mock = AsyncMock(return_value=listed)
    with patch("tp_mcp.tools.workouts.tp_get_workouts", mock):
        out = await lo_get_workouts_summary("2026-09-14", "2026-09-20", workout_filter="completed")
    assert mock.await_args.kwargs["workout_filter"] == "completed"
    assert out["date_range"]["end"] == "2026-09-20"
    assert out["count"] == 1


@pytest.mark.asyncio
async def test_tool_rejects_bad_filter():
    out = await lo_get_workouts_summary("2026-09-14", "2026-09-20", workout_filter="finished")
    assert out["error_code"] == "INVALID_ARGS"


@pytest.mark.asyncio
async def test_tool_propagates_api_error():
    err = {"isError": True, "error_code": "AUTH_EXPIRED", "message": "nope"}
    with patch("tp_mcp.tools.workouts.tp_get_workouts", AsyncMock(return_value=err)):
        out = await lo_get_workouts_summary("2026-09-14", "2026-09-20")
    assert out["error_code"] == "AUTH_EXPIRED"
