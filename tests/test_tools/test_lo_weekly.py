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
                                today="2026-10-03", save_dir=str(tmp_path))
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
    assert row["level"] == "yellow"
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


def test_assess_big_ramp_is_red_and_empty_target_flagged():
    def summ(tri, other=0.0):
        return {"workouts": [], "totals": {"tss_planned_tri": tri, "tss_planned_other": other}}
    assert assess(summ(0), summ(300), summ(420), None)["level"] == "red"          # +40%
    row = assess(summ(0), summ(300), summ(0), None)
    assert "下週未排課" in row["flags"]
    assert assess(summ(0), summ(300), summ(320), None)["level"] == "green"
