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


# ---------------------------------------------------------------------------
# Planned distance from the body — TP's distancePlanned is empty on every
# workout this fork creates, so without this the whole distance basis is dead
# ---------------------------------------------------------------------------

from tp_mcp.tools.lo_review import planned_distance_m  # noqa: E402

_REAL_SWIM = """- 下水前｜肩胸動態熱身：靠牆天使10下、開書式（側臥開書）左右各10下、四足跪姿胸椎伸展左右各10下、棍棒繞肩10個來回
==
- 暖身游200公尺
- 浮板打腿100公尺, 4組
- 指尖划水自由泳50公尺+慢游50公尺
- 25公尺加速游+25公尺放鬆, 2組
==
- 500公尺@80-85%, 2趟, 間休30秒
- 休息1~2分鐘
- 200公尺@85-90%, 5趟, 間休25秒
==
- 緩游200公尺
－－
2x500公尺@80-85%、間休30秒，這一段請把每100公尺壓在1:52~1:55。
5x200公尺@85-90%、間休25秒，每100公尺目標1:44~1:46。"""

_REAL_RUN = """- 伸展
- 8公里@6:35~6:20/km
- 2公里@6:15~5:53/km
- 2公里@6:35~6:20/km
- 2公里@6:15~5:53/km
- 3公里@6:35~6:20/km
- 緩跑5分鐘
- 伸展
－－
第 9 公里起進 6:15~5:53/km 兩公里，每100公尺不要想太多。"""


def test_swim_body_metres_match_the_prescription():
    """The 2026/09/18 閾值間歇: 3000m, which the athlete swam exactly."""
    assert planned_distance_m(_REAL_SWIM) == 3000.0


def test_run_body_kilometres_sum_the_distance_segments():
    """17km of prescribed running; the closing 緩跑5分鐘 carries no distance."""
    assert planned_distance_m(_REAL_RUN) == 17000.0


def test_coaching_writeup_numbers_are_never_counted():
    """Everything after 「－－」 is prose full of 每100公尺 targets."""
    assert planned_distance_m("- 400公尺\n－－\n每100公尺壓在1:52，200公尺後再加速") == 400.0


def test_reps_multiply_only_with_an_explicit_marker():
    assert planned_distance_m("- 100公尺, 4組") == 400.0
    assert planned_distance_m("- 100公尺, 4趟") == 400.0
    assert planned_distance_m("- 100公尺") == 100.0
    # 間休25秒 is not a rep count
    assert planned_distance_m("- 200公尺, 間休25秒") == 200.0


def test_a_rep_split_into_pieces_adds_up_within_the_rep():
    assert planned_distance_m("- 25公尺加速游+25公尺放鬆, 2組") == 100.0


def test_time_only_body_has_no_prescribed_distance():
    assert planned_distance_m("- 伸展\n- 輕鬆慢跑20分鐘，配速不限\n- 伸展") is None
    assert planned_distance_m(None) is None
    assert planned_distance_m("") is None


def test_body_distance_is_used_when_tp_field_is_empty():
    rows = summarize_workouts([{
        "id": "1", "date": "2026-09-18", "sport": "Swim", "title": "閾值間歇",
        "type": "completed", "duration_planned": 1.2, "duration_actual": 1.33,
        "distance_planned_km": None, "distance_actual_km": 3.0,
        "description": _REAL_SWIM,
    }])
    row = rows["workouts"][0]
    assert row["basis"] == "distance"
    assert row["planned_source"] == "body"
    assert (row["planned"], row["actual"], row["unit"]) == (3000, 3000, "m")
    assert row["done_pct"] == 100.0          # time-based read said 110.6%
    assert "incomplete" not in rows


def test_tp_field_wins_when_it_is_populated():
    rows = summarize_workouts([{
        "id": "1", "date": "2026-09-20", "sport": "Run", "title": "長跑",
        "type": "completed", "distance_planned_km": 17.65,
        "distance_actual_km": 17.32, "description": _REAL_RUN,
    }])
    row = rows["workouts"][0]
    assert row["planned_source"] == "tp"
    assert row["planned"] == 17.65


def test_bike_stays_on_time_even_with_distances_in_the_body():
    rows = summarize_workouts([{
        "id": "1", "date": "2026-09-19", "sport": "Bike", "title": "長距離騎乘",
        "type": "completed", "duration_planned": 3.0, "duration_actual": 2.98,
        "distance_actual_km": 95.0, "description": "- 50公里@50-65%",
    }])
    assert rows["workouts"][0]["basis"] == "duration"


def test_device_split_fragment_is_merged():
    from tp_mcp.tools.lo_review import summarize_workouts
    ws = [
        {"id": "a", "date": "2026-09-23", "sport": "Swim", "title": "游泳課堂", "type": "completed",
         "duration_planned": 1.0, "duration_actual": 24.8 / 60, "tss_planned": 45, "tss_actual": 45.7},
        {"id": "b", "date": "2026-09-23", "sport": "Swim", "title": None, "type": "completed",
         "duration_actual": 11.6 / 60, "tss_actual": 16.7},
    ]
    out = summarize_workouts(ws)
    assert out["count"] == 1 and "unplanned" not in out
    assert out["workouts"][0]["actual"] == 36.4
    assert out["merged_fragments"][0]["into"] == "a"
    raw = summarize_workouts(ws, merge_fragments=False)
    assert raw["count"] == 2 and raw["unplanned"]
