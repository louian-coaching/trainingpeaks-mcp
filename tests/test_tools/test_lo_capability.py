"""lo_capability_scan: classification, decoupling verdicts, EF trend, grey zone, wiring."""

from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.tools import lo_capability as cap
from tp_mcp.tools import lo_review, peaks, settings, workouts
from tp_mcp.tools.lo_capability import assess_sport, classify, decoupling_verdict, grey_zone


def test_classify_titles():
    assert classify("Bike", "長距離騎乘（可考慮外騎）") == "steady"
    assert classify("Bike", "有氧耐力") == "steady"
    assert classify("Bike", "輕鬆騎") == "easy"
    assert classify("Bike", "有氧耐力／熱訓練") is None
    assert classify("Bike", "閾值間歇") is None
    assert classify("Run", "長跑") == "steady"
    assert classify("Run", "有氧耐力－越野") == "steady"
    assert classify("Run", "輕鬆跑") == "easy"
    assert classify("Run", "Morning Run") is None
    assert classify("Swim", "有氧耐力") is None


def test_decoupling_verdicts():
    assert "連兩次" in decoupling_verdict(4.0, 3.5)
    assert "再看一週" in decoupling_verdict(4.0, 6.0)
    assert "維持" in decoupling_verdict(6.5, None)
    assert "不加長" in decoupling_verdict(9.1, 2.0)
    assert decoupling_verdict(None, None) is None


def _r(date, dec, ef, dur=120, pause=60, wid=None, temp=None):
    d = {"workout_id": wid or date, "date": date, "title": "長距離騎乘", "duration_min": dur,
         "decoupling_pct": dec, "ef": ef, "if": 0.68, "pause_s": pause}
    if temp is not None:
        d["temp_avg"] = temp
    return d


def test_assess_picks_longest_in_window_and_ef_trend():
    rows = [_r("2026-10-01", 4.2, 1.40, dur=150), _r("2026-09-28", 6.0, 1.30, dur=100),
            _r("2026-09-20", 3.9, 1.33), _r("2026-09-13", 5.5, 1.32), _r("2026-09-06", 4.0, 1.31)]
    a = assess_sport("Bike", rows, "2026-09-21")
    assert a["latest"]["date"] == "2026-10-01"
    assert a["previous"]["date"] == "2026-09-28"
    assert "再看一週" in a["decoupling_verdict"]
    # baseline = sessions older than latest: 1.30, 1.33, 1.32, 1.31 -> 1.315; 1.40/1.315 = +6.5%
    assert a["ef_change_pct"] == 6.5 and a["ef_up"] is True


def test_assess_exclusions():
    rows = [_r("2026-10-01", 9.0, 1.4, pause=900), _r("2026-09-30", 7.0, 1.3, temp=32),
            _r("2026-09-29", None, None), _r("2026-09-28", 4.0, 1.3, dur=70)]
    a = assess_sport("Bike", rows, "2026-09-21")
    reasons = [e["excluded"] for e in a["excluded"]]
    assert any("停頓" in x for x in reasons) and any("均溫" in x for x in reasons)
    assert any("Pw:Hr" in x for x in reasons) and any("未達 90" in x for x in reasons)
    assert "latest" not in a and "note" in a


def test_grey_zone():
    bike = [{"workout_id": "1", "date": "d", "title": "輕鬆騎", "if": 0.81},
            {"workout_id": "2", "date": "d", "title": "有氧耐力", "if": 0.70}]
    run = [{"workout_id": "3", "date": "d", "title": "輕鬆跑", "avg_speed_ms": 1000 / 290}]
    g = grey_zone(bike, run, run_z2_upper_ms=1000 / 300)
    assert [h["workout_id"] for h in g["hits"]] == ["1", "3"] and g["flag"] is True
    assert "4:50/km" in g["hits"][1]["why"] and "5:00/km" in g["hits"][1]["why"]


@pytest.mark.asyncio
async def test_scan_end_to_end(tmp_path):
    listed = {"workouts": [
        {"id": 11, "date": "2026-10-01T00:00:00", "sport": "Bike", "title": "長距離騎乘", "duration_actual": 2.5},
        {"id": 12, "date": "2026-09-24T00:00:00", "sport": "Bike", "title": "長距離騎乘", "duration_actual": 2.0},
        {"id": 13, "date": "2026-09-17T00:00:00", "sport": "Bike", "title": "有氧耐力", "duration_actual": 1.6},
        {"id": 14, "date": "2026-09-10T00:00:00", "sport": "Bike", "title": "有氧耐力", "duration_actual": 1.6},
        {"id": 15, "date": "2026-09-30T00:00:00", "sport": "Bike", "title": "閾值間歇", "duration_actual": 1.2},
        {"id": 16, "date": "2026-09-29T00:00:00", "sport": "Run", "title": "輕鬆跑", "duration_actual": 0.7},
    ]}
    totals = {
        "11": {"Elapsed time": 9100, "Moving time": 9000, "Pw:Hr": 9.4, "EF": 1.45, "IF": 0.70},
        "12": {"Elapsed time": 7300, "Moving time": 7200, "Pw:Hr": 4.0, "EF": 1.30, "IF": 0.69},
        "13": {"Elapsed time": 5800, "Moving time": 5760, "Pw:Hr": 3.0, "EF": 1.31, "IF": 0.68},
        "14": {"Elapsed time": 5800, "Moving time": 5760, "Pw:Hr": 3.5, "EF": 1.29, "IF": 0.67},
        "16": {"Elapsed time": 2550, "Moving time": 2520, "Distance": 8.4, "Pa:Hr": 2.0, "EF": 1.1, "rIF": 0.8},
    }
    st = {"settings": {"powerZones": [{"workoutTypeId": 2, "threshold": 250}],
                       "speedZones": [{"workoutTypeId": 3, "threshold": 3.7,
                                       "zones": [{"maximum": 2.5}, {"maximum": 3.0}]}]}}
    pk = AsyncMock(return_value={"records": [{"value": 275, "date": "2026-09-20", "workout_id": 9}]})
    ident = AsyncMock(return_value={"athlete_name": "Mark Huang", "athlete_id": 5})
    with patch.object(lo_review, "athlete_identity", ident), \
         patch.object(workouts, "tp_get_workouts", AsyncMock(return_value=listed)), \
         patch.object(settings, "tp_get_athlete_settings", AsyncMock(return_value=st)), \
         patch.object(cap, "summary_totals", AsyncMock(side_effect=lambda wid: totals[str(wid)])), \
         patch.object(peaks, "tp_get_peaks", pk):
        r = await cap.lo_capability_scan(["Mark"], end="2026-10-03", save_dir=str(tmp_path))
    row = r["athletes"][0]
    assert row["level"] == "red"
    assert row["bike"]["latest"]["workout_id"] == "11"
    assert "不加長" in row["bike"]["decoupling_verdict"]
    assert any("漂移 9.4%" in f for f in row["flags"])
    assert any("EF" in f for f in row["flags"])        # 1.45 vs ~1.30 baseline
    assert row["peaks"]["ftp_maybe_low"] is True       # 275*0.95=261 >= 250*1.03=257.5
    assert row["grey_zone"]["hits"][0]["sport"] == "Run"  # 8.4km/2520s = 3.33 m/s > 3.0
    assert "閾值間歇" not in str(row)
    assert (tmp_path / "Mark_Huang.json").exists()
    assert r["table"].splitlines()[1].startswith("R|Mark Huang|9.4%")


@pytest.mark.asyncio
async def test_scan_rejects_bad_args():
    assert (await cap.lo_capability_scan([]))["error_code"] == "INVALID_ARGS"
    assert (await cap.lo_capability_scan(["x"], peaks="sometimes"))["error_code"] == "INVALID_ARGS"
