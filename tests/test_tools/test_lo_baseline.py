"""lo_load_baseline — plan from what the coach left on the calendar, not from my own draft."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.client.context import athlete_override
from tp_mcp.tools import lo_sync
from tp_mcp.tools.lo_baseline import lo_load_baseline, week_delta


def _w(date, sport, title, tss=None, dur=1.0):
    return {"id": f"{date}{sport}{title}", "date": date, "title": title, "type": "planned", "sport": sport,
            "duration_planned": dur if sport != "DayOff" else None, "duration_actual": None,
            "distance_planned_km": None, "distance_actual_km": None, "tss_planned": tss, "tss_actual": None,
            "description": ""}


CAL = [
    _w("2026-11-03", "Bike", "有氧耐力", 70), _w("2026-11-05", "Run", "輕鬆跑", 30), _w("2026-11-06", "DayOff", "休息日"),
    _w("2026-11-10", "Bike", "有氧耐力", 80, 1.5), _w("2026-11-12", "Run", "有氧耐力", 45),
    _w("2026-11-13", "Run", "輕鬆跑", 30), _w("2026-11-14", "DayOff", "休息日"), _w("2026-11-15", "Run", "長跑", 100),
    _w("2026-11-16", "DayOff", "休息日"), _w("2026-11-17", "Run", "輕鬆跑", 30), _w("2026-11-18", "Run", "輕鬆跑", 30),
    _w("2026-11-19", "DayOff", "休息日"), _w("2026-11-20", "Run", "輕鬆跑", 30), _w("2026-11-21", "Bike", "有氧耐力", 50),
]
EDITS = [
    {"athlete": "Alice", "kind": "changed", "date": "2026-11-10", "sport": "Bike", "title": "有氧耐力",
     "changes": {"tss_planned": {"from": 60, "to": 80}}},
    {"athlete": "Alice", "kind": "deleted", "date": "2026-11-12", "sport": "Run", "title": "輕鬆跑", "tss_planned": 25},
    {"athlete": "Alice", "kind": "added", "date": "2026-11-12", "sport": "Run", "title": "有氧耐力", "tss_planned": 45},
    {"athlete": "Bob", "kind": "added", "date": "2026-11-12", "sport": "Run", "title": "有氧耐力", "tss_planned": 99},
]


def _fake(start_date, end_date, workout_filter="all"):
    rows = [w for w in CAL if start_date <= w["date"] <= end_date]
    return {"workouts": rows, "count": len(rows)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "coach_edits.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in EDITS),
                                                encoding="utf-8")
    monkeypatch.setattr(lo_sync, "learning_dir", lambda: tmp_path)
    token = athlete_override.set("Alice")
    with patch("tp_mcp.tools.workouts.tp_get_workouts", AsyncMock(side_effect=_fake)), \
         patch("tp_mcp.tools.profile.tp_get_profile", AsyncMock(return_value={"name": "Alice Wang", "athlete_id": 7})):
        yield
    athlete_override.reset(token)


def test_week_delta_telescopes_changed_added_deleted():
    d = week_delta([e for e in EDITS if e["athlete"] == "Alice"], "2026-11-09", "2026-11-15")
    assert d == {"tss": 40.0, "edits": 3, "removed_easy_rest": 1, "added_aerobic": 1}


async def test_baseline_reports_my_plan_vs_the_coachs_week(env):
    out = await lo_load_baseline("2026-11-16", weeks=2, today="2026-11-17")
    w1, w2 = out["weeks"]
    assert (w1["week"], w1["edits"], w1["coach_tri"]) == ("2026-11-02", 0, 100.0)
    assert (w2["mine_tri"], w2["coach_tri"], w2["ratio"]) == (215.0, 255.0, 1.19)
    assert (w2["easy"], w2["rest"], w2["aerobic"]) == (1, 1, 3)
    assert out["norms"]["ratio_median"] == 1.19                 # the unedited week is left out
    assert out["norms"]["easy_plus_rest_median"] == 2
    assert out["norms"]["weeks_used"] == ["2026-11-09"]         # the 100-TSS week is not a normal week
    assert [t["title"] for t in out["aerobic_templates_last_week"]] == ["有氧耐力", "有氧耐力", "長跑"]
    pre = out["precheck"]
    assert (pre["tri"], pre["easy"], pre["rest"]) == (140.0, 3, 2)
    assert (pre["mine_tri"], pre["ratio"], pre["edits"]) == (140.0, 1.0, 0)
    assert len(pre["hints"]) == 2 and "5 堂" in pre["hints"][0]


async def test_no_precheck_when_the_target_week_is_empty(env):
    out = await lo_load_baseline("2026-11-23", weeks=1, today="2026-11-17")
    assert "precheck" not in out and out["weeks"][0]["week"] == "2026-11-16"


async def test_bad_date():
    assert (await lo_load_baseline("nope"))["error_code"] == "INVALID_ARGS"


async def test_one_extra_easy_or_rest_session_is_enough_for_a_hint(env):
    extra = [w for w in CAL if w["date"] <= "2026-11-15"]
    extra += [_w("2026-11-16", "DayOff", "休息日"), _w("2026-11-17", "Run", "輕鬆跑", 30),
              _w("2026-11-18", "Run", "輕鬆跑", 30), _w("2026-11-21", "Bike", "有氧耐力", 200)]   # 3 vs norm 2
    with patch("tp_mcp.tools.workouts.tp_get_workouts",
               AsyncMock(side_effect=lambda start_date, end_date, workout_filter="all": {
                   "workouts": [w for w in extra if start_date <= w["date"] <= end_date]})):
        out = await lo_load_baseline("2026-11-16", weeks=2, today="2026-11-17")
    assert out["precheck"]["easy"] + out["precheck"]["rest"] == 3
    assert any("輕鬆課＋休息日 3 堂" in h for h in out["precheck"]["hints"])
