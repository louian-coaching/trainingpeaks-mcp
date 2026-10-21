"""PROC-84d ledger, SWIM-91 swim metrics, PROC-37b baselines, PER-79 verdicts (2026/10/21)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.tools import analyze, lo_review, lo_weather, peaks, settings, workouts
from tp_mcp.tools import lo_capability as cap
from tp_mcp.tools import lo_progress as lp

# Mark Huang 2026-09-22 閾值間歇 laps (compact; swim "km" = metres, pace_s per 100 m, cad = stroke rate)
_SWIM_LAPS = [
    {"km": 300, "pace_s": 151, "cad": 26},     # warm-up (slow, dropped by 1.35× median? 151 < 1.35*122 → kept)
    {"km": 25, "pace_s": 58, "cad": 8},        # <100 m, dropped
    {"km": 50, "pace_s": 134, "cad": 0},       # kick, dropped
    {"km": 100, "pace_s": 120, "cad": 28}, {"km": 100, "pace_s": 118, "cad": 30},
    {"km": 100, "pace_s": 125, "cad": 29}, {"km": 100, "pace_s": 123, "cad": 29},
    {"km": 200, "pace_s": 129, "cad": 30}, {"km": 200, "pace_s": 122, "cad": 30},
    {"km": 100, "pace_s": 170, "cad": 25},     # cool-down: 170 > 1.35×123 → dropped
]


def test_swim_metrics_from_laps():
    m = lp.swim_metrics({"laps": _SWIM_LAPS})
    assert m["work_laps"] == 7 and m["work_m"] == 1100
    assert 120 < m["pace_s_100"] < 132
    assert m["strokes_per_100"] == pytest.approx(m["stroke_rate"] * m["pace_s_100"] / 60, abs=0.2)
    assert m["dps_m"] == pytest.approx(100 / m["strokes_per_100"], abs=0.01)
    assert lp.swim_metrics({"laps": [{"km": 50, "pace_s": 60, "cad": 30}]}) is None


def test_compare_swim_verdicts():
    hist = [{"swim": {"pace_s_100": 125.0, "dps_m": 1.70}}, {"swim": {"pace_s_100": 124.0, "dps_m": 1.72}}]
    assert lp.compare_swim({"pace_s_100": 121.5, "dps_m": 1.71}, hist)["verdict"] == "進步"
    assert lp.compare_swim({"pace_s_100": 124.5, "dps_m": 1.80}, hist)["verdict"] == "進步"   # same pace, longer stroke
    assert lp.compare_swim({"pace_s_100": 124.5, "dps_m": 1.71}, hist)["verdict"] == "持平"
    assert lp.compare_swim({"pace_s_100": 127.0, "dps_m": 1.70}, hist)["verdict"] == "退步"
    assert lp.compare_swim({"pace_s_100": 120.0}, hist[:1]) is None


def _bike_points(minutes=60, w=180, hr0=130, hr_drift=0.0):
    pts = []
    for i in range(minutes * 6):
        t = i * 10
        pts.append({"time": t, "Power": w, "HeartRate": hr0 + hr_drift * t / (minutes * 60)})
    return pts


def test_baseline_bike_trims_warmup_and_cooldown():
    pts = [{"time": t, "Power": 100, "HeartRate": 100} for t in range(0, 900, 10)]          # 15 min warm-up
    pts += [{"time": 900 + t, "Power": 180, "HeartRate": 130} for t in range(0, 2400, 10)]   # 40 min main
    pts += [{"time": 3300 + t, "Power": 90, "HeartRate": 110} for t in range(0, 300, 10)]   # 5 min cool-down
    m = lp.baseline_metrics("Bike", {}, points=pts)
    assert m["avg_w"] == pytest.approx(180, abs=3) and m["avg_hr"] == pytest.approx(130, abs=2)
    assert abs(m["decoupling_pct"]) < 1.5


def test_baseline_run_uses_distance_trims():
    pts = []
    d = 0.0
    for i in range(0, 3600, 10):
        sp = 3.0 if 700 <= i <= 3200 else 2.5
        d += sp * 10
        pts.append({"time": i, "Distance": d, "Speed": sp, "HeartRate": 150 if sp == 3.0 else 135})
    m = lp.baseline_metrics("Run", {}, points=pts)
    assert m["avg_speed_ms"] == pytest.approx(3.0, abs=0.1) and m["pace_s_km"] == pytest.approx(333, abs=12)


def test_compare_baseline():
    prev = {"avg_w": 180, "avg_hr": 134, "decoupling_pct": 4.0}
    assert lp.compare_baseline("Bike", {"avg_w": 181, "avg_hr": 129, "decoupling_pct": 2.0}, prev)["verdict"] == "進步"
    assert lp.compare_baseline("Bike", {"avg_w": 180, "avg_hr": 138}, prev)["verdict"] == "退步"
    assert lp.compare_baseline("Bike", {"avg_w": 179, "avg_hr": 133}, prev)["verdict"] == "持平"
    assert "不比心率" in lp.compare_baseline("Bike", {"avg_w": 195, "avg_hr": 129}, prev)["verdict"]
    assert "PROC-93" in lp.compare_baseline("Run", {"avg_speed_ms": 3.0, "avg_hr": 140},
                                            {"avg_speed_ms": 3.0, "avg_hr": 150},
                                            {"correct_interpretation": True})["verdict"]
    assert lp.compare_baseline("Bike", {"avg_w": 1}, None)["verdict"].startswith("首次")


def test_ledger_is_idempotent(tmp_path):
    p = tmp_path / "capability_log.jsonl"
    lp.append_ledger([{"athlete_id": "5", "workout_id": "1"}, {"athlete_id": "5", "workout_id": "1"}], path=p)
    lp.append_ledger([{"athlete_id": "5", "workout_id": "1"}, {"athlete_id": "6", "workout_id": "1"}], path=p)
    assert len(p.read_text().splitlines()) == 2
    assert [r["athlete_id"] for r in lp.read_ledger("5", path=p)] == ["5"]


@pytest.mark.asyncio
async def test_scan_progress_layer_logs_weather_swim_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("TP_LO_LEARNING_DIR", str(tmp_path / "learn"))
    # an earlier baseline already in the ledger
    (tmp_path / "learn").mkdir()
    (tmp_path / "learn" / "capability_log.jsonl").write_text(json.dumps({
        "athlete_id": "5", "workout_id": "90", "date": "2026-08-20", "sport": "Bike", "kind": "baseline",
        "metrics": {"avg_w": 180, "avg_hr": 136, "decoupling_pct": 4.0}, "weather": {"outdoor": False}}) + "\n")
    listed = {"workouts": [
        {"id": 21, "date": "2026-10-18T00:00:00", "sport": "Bike", "title": "長距離騎乘", "duration_actual": 2.5},
        {"id": 22, "date": "2026-10-11T00:00:00", "sport": "Bike", "title": "長距離騎乘", "duration_actual": 2.5},
        {"id": 23, "date": "2026-10-15T00:00:00", "sport": "Bike", "title": "基準測驗", "duration_actual": 1.0},
        {"id": 24, "date": "2026-10-16T00:00:00", "sport": "Swim", "title": "閾值間歇", "duration_actual": 1.0},
    ]}
    totals = {"21": {"Elapsed time": 9100, "Moving time": 9000, "Pw:Hr": 9.5, "EF": 1.45},
              "22": {"Elapsed time": 9100, "Moving time": 9000, "Pw:Hr": 4.0, "EF": 1.30}}
    base_pts = [{"time": t, "Power": 100, "HeartRate": 100} for t in range(0, 900, 10)]
    base_pts += [{"time": 900 + t, "Power": 181, "HeartRate": 131} for t in range(0, 2400, 10)]
    base_pts += [{"time": 3300 + t, "Power": 90, "HeartRate": 110} for t in range(0, 300, 10)]
    df = tmp_path / "base.json"
    df.write_text(json.dumps({"data": base_pts}))

    async def fake_analyze(wid, *a, **k):
        if str(wid) == "23":
            return {"data_file": str(df)}
        if str(wid) == "24":
            return {"laps": _SWIM_LAPS}
        return {"data_file": None}

    async def fake_weather(wid, an, athlete=None, **k):
        if str(wid) == "21":
            return {"outdoor": True, "heat_level": "熱", "humid": True, "correct_interpretation": True, "verdict": "熱"}
        return {"outdoor": False}

    st = {"settings": {"powerZones": [{"workoutTypeId": 2, "threshold": 250}]}}
    with patch.object(lo_review, "athlete_identity", AsyncMock(return_value={"athlete_name": "Mark", "athlete_id": 5})), \
         patch.object(workouts, "tp_get_workouts", AsyncMock(return_value=listed)), \
         patch.object(settings, "tp_get_athlete_settings", AsyncMock(return_value=st)), \
         patch.object(cap, "summary_totals", AsyncMock(side_effect=lambda wid: totals.get(str(wid), {}))), \
         patch.object(analyze, "tp_analyze_workout", side_effect=fake_analyze), \
         patch.object(lo_weather, "weather_brief", side_effect=fake_weather), \
         patch.object(peaks, "tp_get_peaks", AsyncMock(return_value={"records": []})):
        r = await cap.lo_capability_scan(["Mark"], end="2026-10-20")
    row = r["athletes"][0]
    # hot 10/18 ride leaves the trend → latest falls back to 10/11 (4.0 %)
    assert row["bike"]["latest"]["workout_id"] == "22"
    assert row["bike"]["excluded"] >= 1
    pr = row["progress"]
    assert pr["baselines"][0]["compare"]["verdict"] == "進步"      # 181 W, HR 131 vs 136
    assert pr["swims"][0]["swim"]["work_laps"] == 7
    assert any("基準測驗 Bike 進步" in f for f in row["flags"])
    lines = (tmp_path / "learn" / "capability_log.jsonl").read_text().splitlines()
    kinds = sorted(json.loads(x)["kind"] for x in lines)
    assert kinds == ["baseline", "baseline", "steady", "steady", "swim"]
    # second run analyses nothing new
    with patch.object(lo_review, "athlete_identity", AsyncMock(return_value={"athlete_name": "Mark", "athlete_id": 5})), \
         patch.object(workouts, "tp_get_workouts", AsyncMock(return_value=listed)), \
         patch.object(settings, "tp_get_athlete_settings", AsyncMock(return_value=st)), \
         patch.object(cap, "summary_totals", AsyncMock(side_effect=lambda wid: totals.get(str(wid), {}))), \
         patch.object(analyze, "tp_analyze_workout", side_effect=AssertionError("re-analysed")), \
         patch.object(lo_weather, "weather_brief", side_effect=AssertionError("re-fetched")), \
         patch.object(peaks, "tp_get_peaks", AsyncMock(return_value={"records": []})):
        r2 = await cap.lo_capability_scan(["Mark"], end="2026-10-20")
    assert r2["athletes"][0]["bike"]["latest"]["workout_id"] == "22"     # hot id still excluded from ledger
    assert len((tmp_path / "learn" / "capability_log.jsonl").read_text().splitlines()) == 5
