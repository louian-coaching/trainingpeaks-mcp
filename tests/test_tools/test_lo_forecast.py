"""lo_weather_forecast (PROC-85b, 2026/10/21)."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from tp_mcp.tools.lo_forecast import (
    assess_day,
    best_window,
    build_days,
    confidence,
    lo_weather_forecast,
    resolve,
)

TODAY = dt.date(2026, 10, 21)


def _hours(day: str, temp, rh, dew, rain_p=0, rain_mm=0.0, gust=20, solar=300):
    out = {k: [] for k in ("time", "temperature_2m", "relative_humidity_2m", "dew_point_2m",
                           "precipitation_probability", "precipitation", "wind_speed_10m",
                           "wind_gusts_10m", "shortwave_radiation")}
    for h in range(24):
        out["time"].append(f"{day}T{h:02d}:00")
        f = (lambda v, h=h: v(h) if callable(v) else v)
        out["temperature_2m"].append(f(temp))
        out["relative_humidity_2m"].append(f(rh))
        out["dew_point_2m"].append(f(dew))
        out["precipitation_probability"].append(f(rain_p))
        out["precipitation"].append(f(rain_mm))
        out["wind_speed_10m"].append(10)
        out["wind_gusts_10m"].append(f(gust))
        out["shortwave_radiation"].append(f(solar))
    return out


def _merge(*hs):
    m = {k: [] for k in hs[0]}
    for h in hs:
        for k, v in h.items():
            m[k] += v
    return m


# Taipei-ish humid warm day, a cool dry day, a rainy windy day
_FC = {
    "timezone": "Asia/Taipei", "utc_offset_seconds": 28800,
    "hourly": _merge(
        _hours("2026-10-26", temp=lambda h: 24 + (6 if 10 <= h <= 15 else 0), rh=80, dew=23),
        _hours("2026-10-27", temp=18, rh=55, dew=9),
        _hours("2026-10-28", temp=21, rh=90, dew=19, rain_p=90, rain_mm=1.5, gust=55),
    ),
}
_AIR = {"hourly": {"time": [f"2026-10-26T{h:02d}:00" for h in range(24)] + [f"2026-10-27T{h:02d}:00" for h in range(24)],
                   "us_aqi": [60] * 24 + [160] * 24, "pm2_5": [10] * 48}}


async def _fetch(url):
    if "air-quality" in url:
        return _AIR
    if "geocoding" in url:
        return {"results": [{"name": "淡水", "latitude": 25.17, "longitude": 121.44, "admin1": "新北", "country": "台灣"}]}
    return _FC


_LOCS = {
    "eric-kao": {"name": "Eric Kao", "place": "新北 淡水", "lat": 25.17, "lon": 121.44, "aliases": ["高"]},
    "lo-u-ian": {"name": "羅譽寅", "place": "台北 北投", "lat": 25.13, "lon": 121.50, "tp_id": "1258179"},
    "same-city": {"name": "X", "place": "新北 淡水", "lat": 25.171, "lon": 121.441},
    "no-coord": {"name": "Y", "place": None, "lat": None, "lon": None},
}


def test_confidence_bands():
    assert [confidence(d) for d in (0, 2, 3, 4, 5, 8)] == ["高", "高", "中", "中", "低", "低"]


def test_resolve_by_slug_name_alias_tpid():
    assert resolve("eric-kao", _LOCS)[0] == "eric-kao"
    assert resolve("Eric Kao", _LOCS)[0] == "eric-kao"
    assert resolve("高", _LOCS)[0] == "eric-kao"
    assert resolve("1258179", _LOCS)[0] == "lo-u-ian"
    assert resolve("nobody", _LOCS) is None


def test_build_days_flags_and_actions():
    days = {d["date"]: d for d in build_days(_FC, _AIR, TODAY)}
    hot = days["2026-10-26"]
    assert hot["heat_level"] in ("熱", "高熱") and hot["humid"]
    assert hot["actions"]["run_quality"] and hot["actions"]["long"]
    # coolest window avoids 10–15 h heat
    assert hot["best_window"]["from"] in ("06:00", "07:00", "16:00", "17:00", "18:00", "19:00")
    cool = days["2026-10-27"]
    assert cool["heat_level"] in ("涼爽", "溫和") and not cool["humid"]
    assert any("AQI" in f for f in cool["flags"]) and cool["actions"]["all"]
    wet = days["2026-10-28"]
    assert any(f.startswith("雨") for f in wet["flags"])
    assert any("陣風" in f for f in wet["flags"])
    assert wet["aqi_max"] is None
    assert any("室內" in a for a in wet["actions"]["bike"])


def test_description_ok_only_when_confident():
    days = {d["date"]: d for d in build_days(_FC, _AIR, TODAY)}
    assert days["2026-10-26"]["confidence"] == "低"          # 5 days out
    assert days["2026-10-26"]["hit"] and not days["2026-10-26"]["description_ok"]
    near = build_days(_FC, _AIR, dt.date(2026, 10, 25))
    d = {x["date"]: x for x in near}["2026-10-26"]
    assert d["confidence"] == "高" and d["description_ok"]


def test_quiet_day_no_actions():
    h = _hours("2026-10-22", temp=19, rh=50, dew=8)
    hrs = [{"hour": int(t[11:13]), "temp": 19, "dew": 8, "wbgt": 15.0, "rain_prob": 0, "rain_mm": 0,
            "gust": 15, "solar": 300} for t in h["time"]]
    d = assess_day("2026-10-22", hrs, [40.0], 1)
    assert d["flags"] == [] and d["actions"] == {} and not d["hit"]


def test_best_window_prefers_dry():
    hrs = [{"hour": h, "wbgt": 20.0 if h < 10 else 24.0, "rain_prob": 90 if h < 10 else 0} for h in range(6, 21)]
    w = best_window(hrs)
    assert not w["wet"] and int(w["from"][:2]) >= 10


@pytest.mark.asyncio
async def test_tool_groups_same_city_and_reports_unresolved():
    res = await lo_weather_forecast(athletes=["eric-kao", "same-city", "no-coord", "ghost"],
                                    start="2026-10-26", days=3, today=TODAY, fetcher=_fetch, locations=_LOCS)
    assert res["success"]
    assert len(res["locations"]) == 1
    assert res["locations"][0]["athletes"] == ["eric-kao", "same-city"]
    assert set(res["unresolved"]) == {"no-coord", "ghost"}
    assert res["locations"][0]["hit_days"] == ["2026-10-26", "2026-10-27", "2026-10-28"]
    assert "WBGT" in res["locations"][0]["line"]


@pytest.mark.asyncio
async def test_place_geocoding_and_start_window():
    res = await lo_weather_forecast(place="淡水", start="2026-10-27", days=2, today=TODAY, fetcher=_fetch)
    loc = res["locations"][0]
    assert "淡水" in loc["place"]
    assert [d["date"] for d in loc["days"]] == ["2026-10-27", "2026-10-28"]


@pytest.mark.asyncio
async def test_rejects_far_start_and_missing_args():
    r = await lo_weather_forecast(lat=25.0, lon=121.0, start="2026-11-05", today=TODAY, fetcher=_fetch)
    assert r["error_code"] == "INVALID_ARGS"
    r = await lo_weather_forecast(today=TODAY, fetcher=_fetch)
    assert r["error_code"] == "INVALID_ARGS"


@pytest.mark.asyncio
async def test_fetch_failure_returns_fallback_urls():
    async def boom(url):
        raise httpx.ConnectError("blocked")
    r = await lo_weather_forecast(lat=25.0, lon=121.0, today=TODAY, fetcher=boom)
    assert r["error_code"] == "WEATHER_FETCH_FAILED" and r["fallback_urls"]


@pytest.mark.asyncio
async def test_air_quality_failure_is_optional():
    async def no_air(url):
        if "air-quality" in url:
            raise httpx.ConnectError("aqi down")
        return _FC
    r = await lo_weather_forecast(lat=25.0, lon=121.0, start="2026-10-26", days=1, today=TODAY, fetcher=no_air)
    assert r["success"] and r["locations"][0]["aqi_available"] is False
