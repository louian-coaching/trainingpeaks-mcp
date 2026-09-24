"""Tests for lo_render.py / lo_sync.py — the coach-edit sync tools (2026/09/23)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.tools.lo_render import (
    compare_body,
    fmt_time,
    line_signature,
    render_body_lines,
    render_for_workout,
    thresholds_from_settings,
)
from tp_mcp.tools.lo_sync import (
    diff_rows,
    lo_delete_workouts_batch,
    lo_diff_week,
    lo_render_body,
    load_snapshot,
    save_snapshot,
)


def step(sec=None, m=None, lo=50, hi=65, cls="active", cad=None):
    ln = {"value": m, "unit": "meter"} if m is not None else {"value": sec, "unit": "second"}
    t = [{"minValue": lo, "maxValue": hi}]
    if cad:
        t.append({"minValue": cad[0], "maxValue": cad[1], "unit": "roundOrStridePerMinute"})
    return {"length": ln, "targets": t, "intensityClass": cls}


def single(st):
    return {"type": "step", "length": {"value": 1, "unit": "repetition"}, "steps": [st]}


def rep(n, *sts):
    return {"type": "repetition", "length": {"value": n, "unit": "repetition"}, "steps": list(sts)}


BIKE = {"bike_ftp": 265.0, "run_pace_sec": 230.0}

# abu wuda 10/03 as the coach rebuilt it in the TP builder
ABU_1003 = {
    "primaryIntensityMetric": "percentOfFtp", "primaryLengthMetric": "duration",
    "structure": [
        single(step(600, lo=50, hi=65, cls="warmUp")),
        single(step(2700, lo=65, hi=75)),
        rep(4, step(360, lo=85, hi=95), step(180, lo=50, hi=60, cls="rest")),
        single(step(2400, lo=65, hi=75)),
        single(step(300, lo=50, hi=60, cls="coolDown")),
    ],
}


# ---------------------------------------------------------------------------
# thresholds
# ---------------------------------------------------------------------------


def test_thresholds_prefer_sport_specific_groups():
    s = {"settings": {
        "powerZones": [{"workoutTypeId": 0, "threshold": 20}, {"workoutTypeId": 2, "threshold": 265},
                       {"workoutTypeId": 3, "threshold": 290}],
        "speedZones": [{"workoutTypeId": 0, "threshold": 2.68}, {"workoutTypeId": 3, "threshold": 4.347826086956522}],
    }}
    th = thresholds_from_settings(s)
    assert th["bike_ftp"] == 265 and th["bike_ftp_source"] == "bike"   # not the stale generic 20W
    assert th["run_ftp"] == 290
    assert round(th["run_pace_sec"]) == 230                              # 3:50/km


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_render_bike_long_ride_with_intervals():
    lines = render_body_lines(ABU_1003, "Bike", BIKE, "長距離騎乘")
    assert lines == [
        "- 熱身騎10分鐘@133~172W",
        "- 45分鐘@172~199W",
        "- 6分鐘@225~252W+恢復3分鐘@133~159W, 4組",
        "- 40分鐘@172~199W",
        "- 緩騎5分鐘@133~159W",
    ]


def test_render_bike_hours_and_cadence():
    sw = {"primaryIntensityMetric": "percentOfFtp", "structure": [
        single(step(600, cls="warmUp")),
        rep(2, step(30, lo=110, hi=115, cad=(100, 120)), step(120, lo=50, hi=60, cls="rest")),
        single(step(3600, lo=65, hi=72)),
        single(step(300, lo=50, hi=60, cls="coolDown")),
    ]}
    lines = render_body_lines(sw, "Bike", BIKE)
    assert lines[1] == "- 30秒@292~305W（100~120rpm）+恢復2分鐘@133~159W, 2組"
    assert lines[2] == "- 1小時@172~191W"                       # BIKE-12


def test_render_run_tempo_distance():
    sw = {"primaryIntensityMetric": "percentOfThresholdPace", "primaryLengthMetric": "distance", "structure": [
        single(step(300, lo=65, hi=72, cls="warmUp")),
        single(step(m=3000, lo=75, hi=85, cls="warmUp")),
        single(step(m=1000, lo=85, hi=90, cls="warmUp")),
        single(step(120, lo=60, hi=65, cls="rest")),
        single(step(m=6000, lo=91, hi=95)),
        single(step(300, lo=68, hi=72, cls="coolDown")),
    ]}
    assert render_body_lines(sw, "Run", BIKE, "節奏跑") == [
        "- 暖身跑5分鐘+伸展",
        "- 3公里@5:07~4:31/km",
        "- 1公里@4:31~4:16/km",
        "- 慢跑恢復2分鐘",
        "- 6公里@4:13~4:02/km",
        "- 緩跑5分鐘",
        "- 伸展",
    ]


def test_render_easy_run_with_strides():
    sw = {"primaryIntensityMetric": "percentOfThresholdPace", "structure": [
        single(step(1500, lo=70, hi=80)),
        rep(3, step(20, lo=95, hi=100), step(100, lo=65, hi=75, cls="rest")),
    ]}
    assert render_body_lines(sw, "Run", {"run_pace_sec": 265}, "輕鬆跑") == [
        "- 伸展",
        "- 輕鬆慢跑25分鐘，配速不限，以能夠只以鼻子呼吸為原則。",
        "- 20秒加速跑, 慢跑恢復1分40秒, 3組",
        "- 伸展",
    ]


def test_fmt_time():
    assert fmt_time(90) == "1分30秒"
    assert fmt_time(4500) == "1小時15分"
    assert fmt_time(4500, big_hours=False) == "75分鐘"


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------


def test_compare_keeps_coach_labels_and_tolerates_rounding():
    """熱身跑 vs 暖身跑 is wording; 4:57 vs 4:59 is % rounding — neither is stale."""
    body = "- 熱身跑5分鐘+伸展\n- 8公里@4:57~4:45/km\n- 伸展\n"
    rendered = ["- 暖身跑5分鐘+伸展", "- 8公里@4:59~4:44/km", "- 伸展"]
    r = compare_body(body, rendered)
    assert r["in_sync"] is True
    assert r["suggested_body"] == body


def test_compare_flags_real_change():
    """赵祎明 9/29: coach stretched the graph to 50 min, text still said 35."""
    body = "- 熱身騎5分鐘@115~138W\n- 35分鐘@138~161W\n- 緩騎5分鐘@115~138W\n"
    rendered = ["- 熱身騎5分鐘@115~138W", "- 50分鐘@138~161W", "- 緩騎5分鐘@115~138W"]
    r = compare_body(body, rendered)
    assert r["in_sync"] is False
    assert {"new": "- 50分鐘@138~161W"} in r["changed_lines"]
    assert {"old": "- 35分鐘@138~161W"} in r["changed_lines"]
    assert "- 50分鐘@138~161W" in r["suggested_body"]


def test_signature_units():
    assert line_signature("- 1小時@172~191W") == line_signature("- 60分鐘@172~191W")
    assert line_signature("- 3公里@5:07~4:31/km") == ("d3000", "p5:07~4:31")


def test_render_for_workout_not_renderable_for_swim():
    assert render_for_workout({"sport": "Swim", "description": "x"}, BIKE)["renderable"] is False


# ---------------------------------------------------------------------------
# snapshot + diff
# ---------------------------------------------------------------------------


def _row(i, date, title="有氧耐力", sport="Bike", desc="- 1小時@172~191W\n－－\n說明", sw=None, tss=50.0):
    return {"id": i, "date": date, "title": title, "sport": sport, "description": desc,
            "tss_planned": tss, "duration_planned": 1.0, "distance_planned_km": None, "structured_workout": sw}


def test_snapshot_window_replace(tmp_path, monkeypatch):
    monkeypatch.setenv("TP_LO_SNAPSHOT_DIR", str(tmp_path))
    save_snapshot([_row("1", "2026-09-28"), _row("2", "2026-10-05")], "2026-09-28", "2026-10-05")
    save_snapshot([_row("3", "2026-09-29")], "2026-09-28", "2026-10-04")
    ws = load_snapshot()["workouts"]
    assert set(ws) == {"2", "3"}          # 1 fell inside the replaced window, 2 did not


def test_diff_added_deleted_moved_changed():
    before = {
        "1": _row("1", "2026-09-29"),
        "2": _row("2", "2026-09-30", sport="Swim", title="閾值耐力"),
        "3": _row("3", "2026-10-01", desc="- 5公里@4:57~4:45/km\n－－\n五公里整段", sport="Run"),
    }
    after = [
        _row("1", "2026-09-29"),
        _row("3", "2026-10-02", desc="- 8公里@4:57~4:45/km\n－－\n五公里整段", sport="Run", tss=48.8),
        _row("9", "2026-10-01", title="有氧耐力"),
    ]
    d = diff_rows(before, after, "2026-09-28", "2026-10-04")
    assert [a["id"] for a in d["added"]] == ["9"]
    assert [x["id"] for x in d["deleted"]] == ["2"]
    ch = d["changed"][0]["changes"]
    assert ch["date"] == {"from": "2026-10-01", "to": "2026-10-02"}
    assert "body" in ch and "explanation" not in ch
    assert ch["tss_planned"]["to"] == 48.8
    assert d["unchanged"] == 1


@pytest.mark.asyncio
async def test_lo_diff_week_reports_stale_body(tmp_path, monkeypatch):
    monkeypatch.setenv("TP_LO_SNAPSHOT_DIR", str(tmp_path))
    old = _row("7", "2026-10-03", title="長距離騎乘",
               desc="- 熱身騎10分鐘@133~172W\n- 2小時@172~191W\n－－\n說明", sw=None)
    save_snapshot([old], "2026-09-28", "2026-10-04")
    new = dict(old, structured_workout=ABU_1003, tss_planned=120.9)
    week = {"success": True, "rows": [new], "week_load": {"tri_tss": 120.9}}
    settings = {"settings": {"powerZones": [{"workoutTypeId": 2, "threshold": 265}], "speedZones": []}}
    with patch("tp_mcp.tools.lo_tools.lo_get_week_for_validate", AsyncMock(return_value=week)), \
         patch("tp_mcp.tools.settings.tp_get_athlete_settings", AsyncMock(return_value=settings)):
        out = await lo_diff_week("2026-09-28", "2026-10-04")
    assert out["changed"][0]["changes"]["structure"] is True
    assert out["stale_bodies"][0]["id"] == "7"
    assert "- 6分鐘@225~252W+恢復3分鐘@133~159W, 4組" in out["stale_bodies"][0]["suggested_body"]
    assert out["snapshot"]


@pytest.mark.asyncio
async def test_lo_render_body_apply_keeps_explanation():
    desc = "- 熱身騎10分鐘@133~172W\n- 2小時@172~191W\n－－\n補給句\n\n說明段"
    detail = {"id": "7", "date": "2026-10-03T00:00:00", "title": "長距離騎乘", "sport": "Bike",
              "description": desc, "metrics": {}, "structured_workout": ABU_1003}
    settings = {"settings": {"powerZones": [{"workoutTypeId": 2, "threshold": 265}]}}
    upd = AsyncMock(return_value={"success": True})
    with patch("tp_mcp.tools.workouts.tp_get_workout", AsyncMock(return_value=detail)), \
         patch("tp_mcp.tools.settings.tp_get_athlete_settings", AsyncMock(return_value=settings)), \
         patch("tp_mcp.tools.lo_tools._update_verified", upd):
        out = await lo_render_body("7", apply=True)
    assert out["applied"] is True
    sent = upd.call_args.kwargs["description"]
    assert sent.endswith("－－\n補給句\n\n說明段")
    assert "- 6分鐘@225~252W+恢復3分鐘@133~159W, 4組" in sent


# ---------------------------------------------------------------------------
# batch delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_batch_verifies_and_skips_completed():
    details = {
        "1": {"id": "1", "date": "2026-09-21", "sport": "Bike", "title": "a", "metrics": {}},
        "2": {"id": "2", "date": "2026-09-22", "sport": "Run", "title": "b", "metrics": {"duration_actual": 0.5}},
    }
    deleted: set[str] = set()

    async def get(workout_id):
        if workout_id in deleted or workout_id not in details:
            return {"isError": True, "message": "Invalid workoutId"}
        return details[workout_id]

    async def delete(workout_id):
        deleted.add(workout_id)
        return {"success": True}

    with patch("tp_mcp.tools.workouts.tp_get_workout", side_effect=get), \
         patch("tp_mcp.tools.workouts.tp_delete_workout", side_effect=delete):
        out = await lo_delete_workouts_batch(["1", "2"])
    st = {r["id"]: r["status"] for r in out["results"]}
    assert st == {"1": "deleted", "2": "skipped_completed"}
    assert out["success"] is True


@pytest.mark.asyncio
async def test_delete_batch_identity_mismatch_deletes_nothing():
    with patch("tp_mcp.tools.profile.tp_get_profile", AsyncMock(return_value={"name": "Yema Selena"})), \
         patch("tp_mcp.tools.workouts.tp_delete_workout", AsyncMock()) as d:
        out = await lo_delete_workouts_batch(["1"], expect_athlete_name="abu wuda")
    assert out["error_code"] == "ATHLETE_MISMATCH"
    d.assert_not_called()


@pytest.mark.asyncio
async def test_delete_batch_dry_run():
    detail = {"id": "1", "date": "2026-09-21", "sport": "Bike", "title": "a", "metrics": {}}
    with patch("tp_mcp.tools.workouts.tp_get_workout", AsyncMock(return_value=detail)), \
         patch("tp_mcp.tools.workouts.tp_delete_workout", AsyncMock()) as d:
        out = await lo_delete_workouts_batch(["1"], dry_run=True)
    assert out["results"][0]["status"] == "would_delete"
    d.assert_not_called()


def test_render_run_strides_keep_target_and_match_both_writings():
    from tp_mcp.tools.lo_render import compare_body
    sw = {"primaryIntensityMetric": "percentOfThresholdPace", "structure": [
        rep(2, step(30, lo=112, hi=117), step(90, lo=50, hi=65, cls="rest")),
    ]}
    lines = render_body_lines(sw, "Run", {"run_pace_sec": 280}, "速度間歇")
    assert lines[0] == "- 30秒@4:10~3:59/km, 慢跑恢復1分30秒, 2組"
    # a body written with the pace, or as bare 衝刺跑, both count as in sync
    assert compare_body("- 30秒@4:10~3:59/km, 慢跑恢復1分30秒, 2組\n- 伸展", lines)["in_sync"]
    assert compare_body("- 30秒衝刺跑, 慢跑恢復1分30秒, 2組\n- 伸展", lines)["in_sync"]
