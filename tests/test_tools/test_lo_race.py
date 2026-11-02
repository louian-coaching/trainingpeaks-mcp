"""PROC-41g (2026/11/02): lo_race_metrics building blocks."""

import pytest

from tp_mcp.tools import lo_race as lr


def test_bike_surges_counts_efforts_and_minutes():
    pts, acc = [], 0.0
    for t in range(0, 3600):
        w = 200.0
        if 600 <= t < 660 or 1200 <= t < 1230:          # 60 s and 30 s at 330 W
            w = 330.0
        acc += w
        pts.append({"time": t, "Power": w, "AccumulatedPower": acc, "HeartRate": 140})
    r = lr.bike_surges(pts, ftp=250)                    # line = 300 W
    assert r["episodes"] == 2
    assert r["minutes_above"] == pytest.approx(1.5, abs=0.15)   # 3-s smoothing trims the edges
    assert r["surge_line_w"] == 300


def _run(km_total, hr_fn, kmh=12.0):
    pts, d = [], 180.0                                   # IM child file: odometer starts at 180 km
    for t in range(0, int(km_total / kmh * 3600) + 1):
        if t:
            d += kmh / 3600
        pts.append({"time": t, "Distance": d, "HeartRate": hr_fn(d - 180.0)})
    return pts


def test_run_decoupling_marathon_bands_and_onset():
    r = lr.run_decoupling(_run(42.2, lambda km: 150 if km < 25 else 150 + (km - 25) * 1.5))
    assert r["scaled_to_distance"] is False and r["ref_km"] == [5.0, 10.0]
    assert 1.10 <= r["ratio"] < 1.20 and r["band"].startswith("中")
    assert 26 <= r["onset_km"] <= 31


def test_run_decoupling_flat_is_low_and_short_is_scaled():
    flat = lr.run_decoupling(_run(21.1, lambda km: 150))
    assert flat["scaled_to_distance"] is True and flat["ratio"] == pytest.approx(1.0, abs=0.01)
    assert flat["band"].startswith("低") and flat["onset_km"] is None


def test_run_decoupling_last_km_spike_is_not_onset():
    r = lr.run_decoupling(_run(21.1, lambda km: 150 if km < 20.2 else 175))
    assert r["onset_km"] is None
