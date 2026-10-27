"""lo_weather_history (PROC-93, 2026/10/17)."""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock, patch

from tp_mcp.tools.lo_weather import (
    build_url,
    cold_level,
    summarise,
    heat_index_c,
    heat_level,
    interpolate,
    lo_weather_history,
    pick_endpoint,
    sample_track,
    wbgt_shade_est,
)

# Kona 2026-10-10, Open-Meteo archive (Pacific/Honolulu, UTC-10), trimmed to 06–15h
_KONA_HOURS = [f"2026-10-10T{h:02d}:00" for h in range(6, 16)]
_KONA = {
    "timezone": "Pacific/Honolulu", "utc_offset_seconds": -36000,
    "hourly": {
        "time": _KONA_HOURS,
        "temperature_2m": [24.7, 24.8, 25.6, 25.9, 26.5, 27.2, 27.8, 27.3, 28.8, 27.4],
        "relative_humidity_2m": [96, 97, 93, 92, 90, 85, 78, 79, 71, 77],
        "dew_point_2m": [24.0, 24.3, 24.4, 24.6, 24.7, 24.4, 23.5, 23.3, 23.0, 23.0],
        "apparent_temperature": [29.0] * 10,
        "wind_speed_10m": [5.7, 6.3, 3.1, 8.0, 10.9, 10.7, 10.5, 10.7, 10.2, 10.6],
        "wind_direction_10m": [90] * 10, "wind_gusts_10m": [20] * 10,
        "shortwave_radiation": [0, 10, 89, 204, 402, 561, 650, 621, 540, 562],
        "precipitation": [0.0] * 10, "cloud_cover": [20] * 10,
    },
}


def test_indices_sane():
    assert 24.5 < wbgt_shade_est(28.0, 75) < 26.5
    assert 18.0 < wbgt_shade_est(22.0, 60) < 20.0
    assert heat_index_c(32.0, 70) > 38          # NOAA table ≈ 40 °C
    assert abs(heat_index_c(20.0, 50) - 20.0) < 1.5
    assert heat_level(17.0) == "涼爽" and heat_level(22.9) == "溫和"
    assert heat_level(25.0) == "熱" and heat_level(28.0) == "高熱" and heat_level(None) is None


def test_endpoint_switches_on_archive_lag():
    today = dt.date(2026, 10, 17)
    assert pick_endpoint(dt.date(2026, 10, 10), today) == "archive"
    assert pick_endpoint(dt.date(2026, 10, 14), today) == "forecast"
    u = build_url(19.64, -155.99, dt.date(2026, 10, 14), "forecast", today)
    assert "api.open-meteo.com/v1/forecast" in u and "past_days=4" in u and "timezone=auto" in u
    a = build_url(19.64, -155.99, dt.date(2026, 10, 9), "archive", today)
    assert "start_date=2026-10-09" in a and "end_date=2026-10-10" in a


def test_interpolate_converts_utc_to_local():
    # 18:30 UTC = 08:30 HST → halfway between 08:00 and 09:00
    row = interpolate(_KONA["hourly"], -36000, dt.datetime(2026, 10, 10, 18, 30, tzinfo=dt.timezone.utc))
    assert row["_local_time"] == "2026-10-10T08:30"
    assert abs(row["temperature_2m"] - 25.75) < 1e-6
    assert row["wind_direction_10m"] == 90


def test_sample_track_spreads_and_skips_bad_fixes():
    pts = [{"time": 0, "PositionLat": 0, "PositionLong": 0}] + [
        {"time": t, "PositionLat": 19.6 + t / 1e5, "PositionLong": -156.0} for t in range(100, 10100, 100)]
    s = sample_track(pts, 3)
    assert [x["offset_s"] for x in s] == [100.0, 5000.0, 10000.0]
    assert sample_track([{"time": 0}], 3) == []


def _patched(tmp_path, data, start="2026-10-10T18:21:15", stop="2026-10-10T23:12:10"):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"startTimestamp": start, "stopTimestamp": stop, "data": data}))
    analysis = {"data_file": str(f), "startTimestamp": start,
                "totals": {"Duration": 17455, "EF": 1.43, "Pw:Hr": 3.04, "NP": 207},
                "channels_min_max_avg": {"Power": [0, 363, 201], "HeartRate": [117, 163, 145]}}
    detail = {"id": "9", "date": "2026-10-10", "sport": "Bike", "title": "Road Cycling"}
    return (patch("tp_mcp.tools.analyze.tp_analyze_workout", AsyncMock(return_value=analysis)),
            patch("tp_mcp.tools.workouts.tp_get_workout", AsyncMock(return_value=detail)))


def _ride():
    return [{"time": t, "PositionLat": 19.64 + t / 1e5, "PositionLong": -155.99, "Temperature": 32}
            for t in range(0, 17460, 60)]


async def test_workout_mode_kona(tmp_path, monkeypatch):
    monkeypatch.setenv("TP_LO_LEARNING_DIR", str(tmp_path / "learn"))
    seen: list[str] = []

    async def fetch(url):
        seen.append(url)
        return _KONA

    a, w = _patched(tmp_path, _ride())
    with a, w:
        res = await lo_weather_history("9", athlete="2154879", today=dt.date(2026, 10, 17), fetcher=fetch)
    assert res["success"] and res["outdoor"]
    assert len(res["samples"]) == 3
    assert res["samples"][0]["local_time"] == "2026-10-10T08:21"
    s = res["summary"]
    assert s["humid"] is True and s["correct_interpretation"] is True
    assert s["heat_level"] in ("熱", "高熱")
    assert s["strong_sun"] is True
    assert "露點" in s["verdict"]
    assert res["device_temp_c"]["mean"] == 32.0
    assert all("archive-api" in u for u in seen)
    log = (tmp_path / "learn" / "heat_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1 and json.loads(log[0])["ef"] == 1.43
    # idempotent
    a, w = _patched(tmp_path, _ride())
    with a, w:
        await lo_weather_history("9", athlete="2154879", today=dt.date(2026, 10, 17), fetcher=fetch)
    assert len((tmp_path / "learn" / "heat_log.jsonl").read_text(encoding="utf-8").splitlines()) == 1


async def test_indoor_returns_outdoor_false(tmp_path):
    a, w = _patched(tmp_path, [{"time": t, "Power": 200} for t in range(0, 600, 60)])
    fetch = AsyncMock()
    with a, w:
        res = await lo_weather_history("9", fetcher=fetch)
    assert res["success"] and res["outdoor"] is False
    fetch.assert_not_called()


async def test_manual_mode_and_fetch_failure_gives_fallback_urls():
    import httpx

    async def boom(url):
        raise httpx.ConnectError("blocked")

    res = await lo_weather_history(lat=25.03, lon=121.56, start="2026-10-15T06:00", duration_min=60,
                                   today=dt.date(2026, 10, 17), fetcher=boom)
    assert res["isError"] and res["error_code"] == "WEATHER_FETCH_FAILED"
    assert res["fallback_urls"] and "past_days" in res["fallback_urls"][0]


async def test_manual_mode_requires_location():
    res = await lo_weather_history(start="2026-10-15T06:00")
    assert res["error_code"] == "INVALID_ARGS"


async def test_zwift_title_is_indoor(tmp_path):
    a, _ = _patched(tmp_path, _ride())
    w = patch("tp_mcp.tools.workouts.tp_get_workout",
              AsyncMock(return_value={"id": "9", "title": "Zwift - Pacer Group Ride with Coco", "sport": "Bike"}))
    fetch = AsyncMock()
    with a, w:
        res = await lo_weather_history("9", fetcher=fetch)
    assert res["outdoor"] is False and "zwift" in res["message"]
    fetch.assert_not_called()


async def test_watopia_gps_is_indoor_even_untitled(tmp_path):
    pts = [{"time": t, "PositionLat": -11.6526, "PositionLong": 166.9797} for t in range(0, 600, 60)]
    a, _ = _patched(tmp_path, pts)
    w = patch("tp_mcp.tools.workouts.tp_get_workout", AsyncMock(return_value={"id": "9", "title": "Ride"}))
    fetch = AsyncMock()
    with a, w:
        res = await lo_weather_history("9", fetcher=fetch)
    assert res["outdoor"] is False and "virtual world" in res["message"]


# --- PROC-93b (2026/10/20): auto-weather block on tp_analyze_workout ---

async def test_weather_brief_reuses_analysis_and_is_compact(tmp_path, monkeypatch):
    from tp_mcp.tools.lo_weather import weather_brief

    monkeypatch.setenv("TP_LO_LEARNING_DIR", str(tmp_path / "learn"))
    a, w = _patched(tmp_path, _ride())

    async def fetch(url):
        return _KONA

    with a as analyze_mock, w:
        analysis = await __import__("tp_mcp.tools.analyze", fromlist=["x"]).tp_analyze_workout("9")
        analyze_mock.reset_mock()
        brief = await weather_brief("9", analysis, athlete="2154879",
                                    today=dt.date(2026, 10, 17), fetcher=fetch)
        analyze_mock.assert_not_called()          # no second analysis call
    assert brief["outdoor"] is True and brief["correct_interpretation"] is True
    assert brief["humid"] is True and "露點" in brief["verdict"]
    assert "samples" not in brief


async def test_weather_brief_never_raises(tmp_path):
    import httpx

    from tp_mcp.tools.lo_weather import weather_brief

    a, w = _patched(tmp_path, _ride())

    async def boom(url):
        raise httpx.ConnectError("blocked")

    with a, w:
        analysis = {"data_file": str(tmp_path / "a.json"), "startTimestamp": "2026-10-10T18:21:15"}
        brief = await weather_brief("9", analysis, today=dt.date(2026, 10, 17), fetcher=boom)
    assert brief["outdoor"] is None and brief["error"] == "WEATHER_FETCH_FAILED"
    assert brief["fallback_urls"]


async def test_weather_brief_indoor(tmp_path):
    from tp_mcp.tools.lo_weather import weather_brief

    a, w = _patched(tmp_path, [{"time": t, "Power": 200} for t in range(0, 600, 60)])
    fetch = AsyncMock()
    with a, w:
        analysis = {"data_file": str(tmp_path / "a.json"), "startTimestamp": "2026-10-10T18:21:15"}
        brief = await weather_brief("9", analysis, fetcher=fetch)
    assert brief["outdoor"] is False
    fetch.assert_not_called()


def test_cold_level_threshold():
    """PROC-93c: mean air temp ≤5 °C = 冷天."""
    assert cold_level(None) is None
    assert cold_level(5.0) == "冷"
    assert cold_level(-8.2) == "冷"
    assert cold_level(5.1) is None


def _wx(temp: float) -> dict:
    return {"temp_c": temp, "dew_point_c": temp - 6, "rh_pct": 60, "wbgt_shade_est_c": temp - 2,
            "heat_index_c": temp, "wind_kmh": 12, "solar_wm2": 200}


def test_summarise_cold_session_corrects_interpretation():
    s = summarise([_wx(2.0), _wx(3.0), _wx(4.0)], None)
    assert s["cold_level"] == "冷"
    assert s["heat_level"] == "涼爽"
    assert s["correct_interpretation"] is True
    assert "冷天" in s["verdict"]


def test_summarise_cool_not_cold():
    s = summarise([_wx(8.0), _wx(10.0)], None)
    assert s["cold_level"] is None
    assert s["correct_interpretation"] is False
    assert "冷天" not in s["verdict"]

