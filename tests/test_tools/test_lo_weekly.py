"""Tests for tools/lo_weekly.py — the one-call 週檢."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.client.context import athlete_override
from tp_mcp.tools.lo_weekly import assess, lo_weekly_check


def _w(date, sport="Bike", type_="planned", tss_p=None, tss_a=None, dur_p=1.0, dur_a=None, title="課"):
    return {"id": f"{date}{sport}{title}", "date": date, "title": title, "type": type_, "sport": sport,
            "duration_planned": dur_p, "duration_actual": dur_a, "distance_planned_km": None,
            "distance_actual_km": None, "tss_planned": tss_p, "tss_actual": tss_a, "description": ""}


# Week data keyed by window start, per athlete.
WEEKS = {
    "Alice": {
        "2026-09-28": [_w("2026-09-29", tss_p=100, type_="completed", dur_a=1.0, tss_a=95),
                       _w("2026-09-30", sport="Run", tss_p=60, title="節奏跑"),        # missed
                       _w("2026-10-03", sport="Swim", tss_p=40, title="有氧游"),       # pending today
                       _w("2026-10-04", tss_p=200, title="長騎"),
                       _w("2026-10-01", sport="Strength", tss_p=50, title="肌力", type_="completed", dur_a=1.0)],
        "2026-10-05": [_w("2026-10-07", tss_p=230), _w("2026-10-09", tss_p=230),
                       _w("2026-10-08", sport="Strength", tss_p=80, title="肌力")],
    },
}


def _fake_workouts(start_date, end_date, workout_filter="all"):
    who = athlete_override.get()
    data = WEEKS.get(who, {})
    rows = [w for start, ws in data.items() for w in ws if start_date <= w["date"] <= end_date]
    return {"workouts": rows, "count": len(rows)}


def _fake_profile():
    who = athlete_override.get()
    if who == "Nobody":
        return {"isError": True, "error_code": "NOT_FOUND", "message": "no"}
    return {"name": {"alice": "Alice Wang", "bob": "Robert Lin"}.get(who.lower(), who), "athlete_id": 7}


FIT = {"current": {"ctl": 60.0, "atl": 85.0, "tsb": -25.0},
       "daily_data": [{"ctl": 57.0}, {"ctl": 60.0}]}


@pytest.fixture
def patched():
    with patch("tp_mcp.tools.workouts.tp_get_workouts", AsyncMock(side_effect=_fake_workouts)), \
         patch("tp_mcp.tools.profile.tp_get_profile", AsyncMock(side_effect=_fake_profile)), \
         patch("tp_mcp.tools.fitness.tp_get_fitness", AsyncMock(return_value=FIT)) as fit:
        yield fit


@pytest.mark.asyncio
async def test_one_call_covers_review_load_and_fitness(patched, tmp_path):
    out = await lo_weekly_check(["Alice"], "2026-09-28", "2026-10-03", "2026-10-05", "2026-10-11",
                                today="2026-10-03", save_dir=str(tmp_path), detail="rows")
    assert out["windows"]["prev_full_week"] == ["2026-09-28", "2026-10-04"]
    row = out["athletes"][0]
    assert row["athlete_name"] == "Alice Wang"
    assert row["review"]["missed"] == ["09-30 Run 節奏跑"]
    assert row["review"]["pending_today"] == ["Swim 有氧游"]
    # previous FULL week (incl. Sunday 長騎), tri only — strength 50 left out
    assert row["load"]["prev_week_planned_tri"] == 400
    assert row["load"]["target_planned_tri"] == 460
    assert row["load"]["change_pct"] == 15.0
    assert row["load"]["target_planned_other"] == 80
    assert row["fitness"]["ctl_change_7d"] == 3.0
    assert "TSB -25.0" in row["flags"] and "漏課1堂" in row["flags"]
    # 11/17: one of the two tri sessions due so far was missed = 50% by volume (< 70%) → red;
    # today's swim is pending, not part of the denominator.
    assert row["review"]["volume_pct"] == 50 and row["level"] == "red"
    saved = json.loads((tmp_path / "Alice_Wang.json").read_text())
    assert saved["target"]["totals"]["tss_planned_tri"] == 460
    assert "Alice Wang" in out["table"]
    patched.assert_awaited_with(start_date="2026-09-26", end_date="2026-10-03")


@pytest.mark.asyncio
async def test_unresolvable_athlete_is_an_error_row_not_a_crash(patched):
    out = await lo_weekly_check(["Alice", "Nobody"], "2026-09-28", "2026-10-03",
                                "2026-10-05", "2026-10-11", today="2026-10-03")
    assert out["athletes"][0]["athlete"] == "Nobody"       # errors sort first
    assert out["athletes"][0]["level"] == "error"
    assert out["levels"]["error"] == 1 and out["count"] == 2


@pytest.mark.asyncio
async def test_identity_mismatch_is_flagged_red(patched):
    out = await lo_weekly_check(["Bob"], "2026-09-28", "2026-10-03", "2026-10-05", "2026-10-11",
                                today="2026-10-03")
    row = out["athletes"][0]
    assert row["level"] == "red" and row["flags"][0].startswith("身分待確認")


@pytest.mark.asyncio
async def test_override_is_restored_after_the_batch(patched):
    assert athlete_override.get() is None
    await lo_weekly_check(["Alice"], "2026-09-28", "2026-10-03", "2026-10-05", "2026-10-11")
    assert athlete_override.get() is None


@pytest.mark.asyncio
async def test_bad_dates_and_empty_list_are_rejected():
    assert (await lo_weekly_check([], "2026-09-28", "2026-10-03", "2026-10-05", "2026-10-11"))["isError"]
    assert (await lo_weekly_check(["A"], "2026-9-28", "x", "2026-10-05", "2026-10-11"))["isError"]


def _summ(tri, other=0.0, workouts=(), **lists):
    return {"workouts": list(workouts), "totals": {"tss_planned_tri": tri, "tss_planned_other": other}, **lists}


def _row(date, sport, title, planned, unit, done_pct=None, type_="planned", tss_a=None):
    r = {"date": date, "sport": sport, "title": title, "planned": planned, "unit": unit, "type": type_}
    if done_pct is not None:
        r["done_pct"] = done_pct
        r["type"] = "completed"
    if tss_a is not None:
        r["tss_actual"] = tss_a
    return r


def test_assess_ramp_is_a_signal_only_when_last_week_was_not_absorbed():
    done = [_row("2026-11-10", "Bike", "閾值間歇", 60, "min", 100.0)]
    half = [_row("2026-11-10", "Bike", "閾值間歇", 60, "min", 100.0),
            _row("2026-11-12", "Run", "節奏跑", 10, "km")]
    miss = [{"date": "2026-11-12", "sport": "Run", "title": "節奏跑", "planned": 10, "unit": "km"}]
    ok = assess(_summ(0, workouts=done), _summ(300), _summ(420), None)             # +40%, 100% done
    assert ok["level"] == "green" and "下週tri +40%" in ok["notes"]
    bad = assess(_summ(0, workouts=half, missed=miss), _summ(300), _summ(420), None)  # 52% done
    assert bad["level"] == "red" and "下週tri +40%" in bad["flags"]
    assert bad["review"]["volume_pct"] == 52
    row = assess(_summ(0), _summ(300), _summ(0), None)
    assert "下週未排課" in row["flags"]
    assert assess(_summ(0), _summ(300), _summ(320), None)["level"] == "green"
    assert assess(_summ(0), _summ(400), _summ(300), None)["flags"] == []            # smaller week: never a flag


def test_assess_unpaired_file_counts_as_done_same_day_then_adjacent_day():
    ws = [_row("2026-11-10", "Bike", "有氧耐力", 75, "min"), _row("2026-11-14", "Bike", "長距離騎乘", 90, "min")]
    missed = [{"date": w["date"], "sport": "Bike", "title": w["title"], "planned": w["planned"], "unit": "min"}
              for w in ws]
    unplanned = [{"date": "2026-11-11", "sport": "Bike", "title": "Road Cycling", "actual": 89.8, "unit": "min"},
                 {"date": "2026-11-14", "sport": "Bike", "title": "Road Cycling", "actual": 45.0, "unit": "min"},
                 {"date": "2026-11-12", "sport": "Other", "title": "Yoga", "actual": 20.0, "unit": "min"},
                 {"date": "2026-11-13", "sport": "Run", "title": "Running", "actual": 8.0, "unit": "min"}]
    r = assess(_summ(0, workouts=ws, missed=missed, unplanned=unplanned), _summ(200), _summ(200), None)
    assert r["review"]["missed"] == [] and len(r["review"]["matched_unplanned"]) == 2
    assert r["review"]["volume_pct"] == 73                     # (75 + 45) / 165
    assert r["review"]["unplanned"] == [] and r["review"]["other_activities"] == 1
    assert r["flags"] == [] and "短活動1筆" in r["notes"] and r["level"] == "green"


def test_assess_week_types_and_key_sessions():
    easy = [{"date": "2026-11-13", "sport": "Run", "title": "輕鬆跑", "planned": 40, "unit": "min"},
            {"date": "2026-11-13", "sport": "Strength", "title": "力量訓練", "planned": 60, "unit": "min"}]
    big = [_row("2026-11-10", "Bike", "長距離騎乘", 300, "min", 100.0), _row("2026-11-13", "Run", "輕鬆跑", 40, "min")]
    r = assess(_summ(0, workouts=big, missed=easy), _summ(300), _summ(300), None)
    assert r["level"] == "yellow" and "漏課2堂" in r["flags"]                       # two easy misses: not red
    off = assess(_summ(0, workouts=big, missed=easy), _summ(100), _summ(300), None, week_type="off_season")
    assert off["level"] == "green" and "可選課未做2堂" in off["notes"]
    gap = assess(_summ(0, workouts=big, missed=easy), _summ(100), _summ(300), None, week_type="data_gap")
    assert gap["flags"] == ["資料缺口"] and gap["level"] == "yellow"
    race = assess(_summ(0, workouts=[_row("2026-11-10", "Run", "節奏跑", 10, "km", 60.0)]),
                  _summ(100), _summ(300), None, week_type="post_race")
    assert "下週tri +200%" in race["notes"] and race["level"] == "green"


def test_assess_flags_identical_tss_as_duplicate_upload():
    ws = [_row("2026-11-15", "Run", "有氧耐力", 14, "km", 100.1, tss_a=90.95),
          {"date": "2026-11-15", "sport": "Run", "title": "Running", "planned": None, "unit": "min",
           "actual": 72.4, "type": "completed", "tss_actual": 90.95}]
    r = assess(_summ(0, workouts=ws), _summ(100), _summ(100), None)
    assert r["review"]["duplicates"] == ["11-15 Run Running"] and "疑似重複檔1筆" in r["flags"]


@pytest.mark.asyncio
async def test_with_save_dir_the_response_is_the_table_only(patched, tmp_path):
    out = await lo_weekly_check(["Alice"], "2026-09-28", "2026-10-03", "2026-10-05", "2026-10-11",
                                today="2026-10-03", save_dir=str(tmp_path), week_types={"Alice": "post_race"})
    assert "athletes" not in out and out["errors"] == [] and "賽後" in out["table"]
    saved = json.loads((tmp_path / "Alice_Wang.json").read_text())
    assert saved["check"]["week_type"] == "post_race"
    bad = await lo_weekly_check(["Alice"], "2026-09-28", "2026-10-03", "2026-10-05", "2026-10-11",
                                week_types={"Alice": "holiday"})
    assert bad["error_code"] == "INVALID_ARGS"
