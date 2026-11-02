"""2026/11/02: PROC-84h segments, PROC-85g tiers, PROC-84g gates, PER-79b, PER-79c."""

import pytest

from tp_mcp.tools import lo_capability as cap
from tp_mcp.tools import lo_capability_ext as ext


def _ride(seconds, watts=200.0, hr_fn=None, kmh=30.0, stop=None, warm_hr=None):
    """1 s TP-style ride: Distance km, AccumulatedPower J."""
    pts, d, acc = [], 0.0, 0.0
    for t in range(seconds + 1):
        moving = not (stop and stop[0] <= t < stop[1])
        if t:
            d += (kmh / 3600) if moving else 0.0
            acc += watts if moving else 0.0
        hr = hr_fn(t) if hr_fn else 140
        if warm_hr is not None and t < 600:
            hr = warm_hr
        pts.append({"time": t, "Distance": d, "AccumulatedPower": acc, "Power": watts if moving else 0,
                    "HeartRate": hr})
    return pts


def test_segment_short_ride_front_only_and_warmup_dropped():
    # 100 min steady, HR 100 in the first 10 min would fake a big drift if kept
    out = ext.segment_decoupling(_ride(6000, warm_hr=100), "Bike")
    assert out["long"] is False and "back" not in out
    assert abs(out["front"]["decoupling_pct"]) < 0.5


def test_segment_long_ride_back_durability_and_drift():
    # 3 h at 200 W; HR 140 for 2 h, then 155 → back EF ~10% below the front
    out = ext.segment_decoupling(_ride(10800, hr_fn=lambda t: 140 if t < 7200 else 155), "Bike",
                                 vt1_hr=150, z2_upper=205)
    assert out["long"] is True
    assert abs(out["front"]["decoupling_pct"]) < 0.5           # 加長判準 reads the clean front
    assert out["back"]["durability_pct"] == pytest.approx(10.7, abs=0.5)
    assert out["back"]["drift_into_heavy"] is True             # 155 > 150 and 200 W ≥ 0.97 × 205


def test_segment_stops_removed():
    # a 15-min café stop in the middle (HR 95, no power) must not move the numbers
    out = ext.segment_decoupling(_ride(6000, stop=(3000, 3900),
                                       hr_fn=lambda t: 95 if 3000 <= t < 3900 else 140), "Bike")
    assert abs(out["front"]["decoupling_pct"]) < 0.5


def test_segment_run_km_units():
    pts, d = [], 0.0
    for t in range(0, 5401):
        if t:
            d += 10.8 / 3600                                     # 3 m/s, Distance in km
        pts.append({"time": t, "Distance": d, "HeartRate": 140 + (8 if t > 3300 else 0)})
    out = ext.segment_decoupling(pts, "Run")
    assert out["front"]["halves"][0]["avg_speed_ms"] == pytest.approx(3.0, abs=0.02)
    assert out["front"]["decoupling_pct"] > 3


def test_effective_decoupling_prefers_front():
    assert ext.effective_decoupling({"decoupling_pct": 9.0, "seg": {"front": {"decoupling_pct": 3.1}}}) \
        == (3.1, "前段（PROC-84h）")
    assert ext.effective_decoupling({"decoupling_pct": 9.0}) == (9.0, "TP 摘要")


def test_assess_uses_front_and_flags_durability():
    rows = [
        {"workout_id": "1", "date": "2026-11-01", "duration_min": 200, "decoupling_pct": 11.0, "ef": 1.4,
         "seg": {"front": {"decoupling_pct": 3.0}, "back": {"durability_pct": 9.5, "drift_into_heavy": True,
                                                           "avg_hr": 152, "vt1_hr": 148}}},
        {"workout_id": "2", "date": "2026-10-25", "duration_min": 190, "decoupling_pct": 4.0, "ef": 1.38,
         "seg": {"front": {"decoupling_pct": 4.0}, "back": {"durability_pct": 8.6}}},
    ]
    a = cap.assess_sport("Bike", rows, "2026-10-20")
    assert a["decoupling_used"] == 3.0 and "連兩次" in a["decoupling_verdict"]
    assert a["durability"]["weak"] is True
    assert a["drift_into_heavy"]["vt1_hr"] == 148


def test_ftp_tiers():
    assert ext.ftp_tier(265, 250)["report"] is False                      # +0.7%
    t = ext.ftp_tier(275, 250)                                             # +4.5%, one source
    assert t["report"] is False and "需第二來源" in t["tier"]
    assert ext.ftp_tier(275, 250, second_w=273)["report"] is True         # 2nd +3.7%
    assert ext.ftp_tier(290, 250)["report"] is True                       # +10.2%


def test_ef_gates():
    assert ext.ef_gates([176], [183, 182], 5, [5, 5])["pass"] is False    # peak HR −6.5
    assert ext.ef_gates([181], [183, 182], 7, [5, 5.5])["pass"] is False  # RPE +1.75
    assert ext.ef_gates([181], [183, 182], 5, [5, 5])["pass"] is True
    assert ext.ef_gates([], [183], None, [])["pass"] is None
    assert ext.is_quality("閾值間歇") and not ext.is_quality("基準測驗") and not ext.is_quality("有氧耐力")


def test_easy_run_index():
    base = [{"date": f"2026-10-{d}", "avg_hr": hr, "avg_speed_ms": 3.0} for d, hr in
            (("10", 140), ("12", 142), ("15", 141), ("18", 139), ("21", 140))]
    week = [{"date": "2026-10-28", "avg_hr": 147, "avg_speed_ms": 3.0},
            {"date": "2026-10-30", "avg_hr": 146, "avg_speed_ms": 3.0}]
    r = ext.easy_run_index(week, base)
    assert r["abnormal"] is True and r["hr_delta_bpm_at_base_pace"] == pytest.approx(6.1, abs=0.3)
    assert ext.easy_run_index(week[:1], base)["abnormal"] is None
    hot = [{**week[0], "excluded": "熱"}, {**week[1], "excluded": "熱"}]
    assert ext.easy_run_index(hot, base)["abnormal"] is None
    assert ext.is_easy_run("輕鬆跑") and not ext.is_easy_run("長跑") and not ext.is_easy_run("閾值間歇")


def test_wellness_signal():
    daily = [{"date": f"2026-10-{d:02d}", "rhr": 50, "hrv": 60 + (d % 3)} for d in range(1, 26)]
    daily += [{"date": f"2026-10-{d:02d}", "rhr": 54, "hrv": 61} for d in range(26, 32)]
    w = ext.wellness_signal(daily, "2026-10-31")
    assert w["rhr"]["yellow"] is True and any("靜止心率" in f for f in w["flags"])
    sat = [{"date": f"2026-10-{d:02d}", "rhr": 50, "hrv": 60 + (d % 3)} for d in range(1, 26)]
    sat += [{"date": f"2026-10-{d:02d}", "rhr": 47, "hrv": 70} for d in range(26, 32)]
    s = ext.wellness_signal(sat, "2026-10-31")
    assert s["hrv"]["saturation_candidate"] is True
    few = ext.wellness_signal([{"date": "2026-10-30", "hrv": 60}], "2026-10-31")
    assert "不判" in few["hrv"]["note"] and not few["flags"]
