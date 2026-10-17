"""羅教練 fork-only: ``lo_weather_history`` — what was the weather during this session?

**Context (PROC-93, 2026/10/17 教練裁示).** Reviews of past sessions and races
had no weather source: the only numbers were the head-unit temperature (sun +
body heat push it 3–8 °C above air) or the athlete's word. Kona 2026 showed why
that matters — air was only 26–29 °C on the bike, but the dew point sat at
23–25 °C, which is what cost 10–15 % power at the same heart rate.

What it does, in one call:

1. ``workout_id`` → reads the activity file via ``tp_analyze_workout``: UTC
   start/stop and the GPS track. No GPS ⇒ ``outdoor: false`` (Zwift, treadmill,
   pool) unless the caller passes ``lat``/``lon`` explicitly.
2. Samples up to ``n_points`` positions along the track at their real times
   (a 180 km course crosses several micro-climates), asks Open-Meteo for the
   hourly history at each (``archive-api`` once ERA5 has the day, the forecast
   API's ``past_days`` for the last few days) and interpolates to the minute.
3. Derives heat index and a shade WBGT estimate (0.7·Tw + 0.3·Ta with Stull's
   wet-bulb — no radiation term, so strong sun is flagged separately), a heat level, and a
   ``verdict`` line the review can quote.
4. Appends one line per outdoor session to ``heat_log.jsonl`` in the learning
   directory (EF / Pw:Hr / avg power / avg HR against dew point and WBGT) — the
   per-athlete heat-tolerance record PROC-93 asks for.

Network: the request goes out from the machine running this server. If it is
blocked, the error carries ``fallback_urls`` — the exact Open-Meteo calls — so
the session can fetch them another way (e.g. Firecrawl) and keep the same
numbers.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARS = (
    "temperature_2m", "relative_humidity_2m", "dew_point_2m", "apparent_temperature",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "shortwave_radiation",
    "precipitation", "cloud_cover",
)
ARCHIVE_LAG_DAYS = 5          # ERA5 lands ~5 days late; newer days come from the forecast API
TIMEOUT_S = 20.0

# Heat levels on the shade-WBGT estimate (°C). Same cut points as PROC-93 in
# method/9 §9.6 — change both together.
WBGT_LEVELS = ((18.0, "涼爽"), (23.0, "溫和"), (28.0, "熱"), (math.inf, "高熱"))
HUMID_DEW_C = 20.0            # dew point at or above this = 濕熱 flag
STRONG_SUN_WM2 = 600.0        # shortwave radiation; shade WBGT under-reads by ~2–3 °C above this


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message, **extra}


# ---------------------------------------------------------------------------
# Thermal indices
# ---------------------------------------------------------------------------
def vapour_pressure_hpa(t_c: float, rh: float) -> float:
    return rh / 100.0 * 6.105 * math.exp(17.27 * t_c / (237.7 + t_c))


def wet_bulb_stull(t_c: float, rh: float) -> float:
    """Stull (2011) psychrometric wet-bulb from air temp and RH (±0.3 °C for RH 5–99 %)."""
    return (t_c * math.atan(0.151977 * math.sqrt(rh + 8.313659)) + math.atan(t_c + rh)
            - math.atan(rh - 1.676331) + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) - 4.686035)


def wbgt_shade_est(t_c: float, rh: float) -> float:
    """Shade WBGT ≈ 0.7·Tw + 0.3·Ta (ISO 7243 no-sun form, Tw from Stull). Full sun adds
    roughly 2–3 °C — flagged separately as ``strong_sun``."""
    return 0.7 * wet_bulb_stull(t_c, rh) + 0.3 * t_c


def heat_index_c(t_c: float, rh: float) -> float:
    """NOAA Rothfusz regression (with the simple formula below 26.7 °C)."""
    t_f = t_c * 9 / 5 + 32
    simple = 0.5 * (t_f + 61.0 + (t_f - 68.0) * 1.2 + rh * 0.094)
    if (simple + t_f) / 2 < 80:
        hi_f = simple
    else:
        hi_f = (-42.379 + 2.04901523 * t_f + 10.14333127 * rh - 0.22475541 * t_f * rh
                - 0.00683783 * t_f * t_f - 0.05481717 * rh * rh + 0.00122874 * t_f * t_f * rh
                + 0.00085282 * t_f * rh * rh - 0.00000199 * t_f * t_f * rh * rh)
        if rh < 13 and 80 <= t_f <= 112:
            hi_f -= ((13 - rh) / 4) * math.sqrt((17 - abs(t_f - 95)) / 17)
        elif rh > 85 and 80 <= t_f <= 87:
            hi_f += ((rh - 85) / 10) * ((87 - t_f) / 5)
    return (hi_f - 32) * 5 / 9


def heat_level(wbgt: float | None) -> str | None:
    if wbgt is None:
        return None
    for cut, name in WBGT_LEVELS:
        if wbgt < cut:
            return name
    return WBGT_LEVELS[-1][1]


# ---------------------------------------------------------------------------
# Track sampling and time handling
# ---------------------------------------------------------------------------
def parse_utc(s: str | None) -> dt.datetime | None:
    """TP's analysis timestamps are UTC without an offset ('2026-10-10T18:21:15')."""
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def sample_track(points: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Up to ``n`` GPS fixes spread evenly over elapsed time (first and last included)."""
    fixes = [p for p in points
             if isinstance(p.get("PositionLat"), (int, float)) and isinstance(p.get("PositionLong"), (int, float))
             and not (p["PositionLat"] == 0 and p["PositionLong"] == 0)]
    if not fixes:
        return []
    n = max(1, n)
    if n == 1 or len(fixes) == 1:
        idx = [0]
    else:
        t0 = fixes[0].get("time") or 0
        t1 = fixes[-1].get("time") or 0
        idx = []
        for k in range(n):
            target = t0 + (t1 - t0) * k / (n - 1)
            j = min(range(len(fixes)), key=lambda i: abs((fixes[i].get("time") or 0) - target))
            if j not in idx:
                idx.append(j)
    return [{"offset_s": float(fixes[i].get("time") or 0), "lat": float(fixes[i]["PositionLat"]),
             "lon": float(fixes[i]["PositionLong"])} for i in idx]


def pick_endpoint(day: dt.date, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    return "archive" if (today - day).days > ARCHIVE_LAG_DAYS else "forecast"


def build_url(lat: float, lon: float, day: dt.date, kind: str, today: dt.date | None = None) -> str:
    hourly = ",".join(HOURLY_VARS)
    base = f"latitude={lat:.4f}&longitude={lon:.4f}&hourly={hourly}&timezone=auto"
    if kind == "archive":
        return f"{ARCHIVE_URL}?{base}&start_date={day.isoformat()}&end_date={(day + dt.timedelta(days=1)).isoformat()}"
    today = today or dt.date.today()
    past = max(1, min(92, (today - day).days + 1))
    return f"{FORECAST_URL}?{base}&past_days={past}&forecast_days=1"


def interpolate(hourly: dict[str, list[Any]], utc_offset_s: int, when_utc: dt.datetime) -> dict[str, float | None]:
    """Linear interpolation of every hourly variable at ``when_utc``."""
    times = hourly.get("time") or []
    local = (when_utc + dt.timedelta(seconds=utc_offset_s)).replace(tzinfo=None)
    stamps = [dt.datetime.fromisoformat(t) for t in times]
    out: dict[str, float | None] = {}
    if not stamps:
        return dict.fromkeys(HOURLY_VARS)
    j = 0
    while j + 1 < len(stamps) and stamps[j + 1] <= local:
        j += 1
    k = min(j + 1, len(stamps) - 1)
    span = (stamps[k] - stamps[j]).total_seconds()
    frac = 0.0 if span <= 0 else max(0.0, min(1.0, (local - stamps[j]).total_seconds() / span))
    for v in HOURLY_VARS:
        series = hourly.get(v) or []
        a = series[j] if j < len(series) else None
        b = series[k] if k < len(series) else None
        if a is None and b is None:
            out[v] = None
        elif a is None or b is None:
            out[v] = float(a if a is not None else b)
        elif v == "wind_direction_10m":
            out[v] = float(a if frac < 0.5 else b)          # don't average angles
        else:
            out[v] = float(a) + (float(b) - float(a)) * frac
    out["_local_time"] = local.isoformat(timespec="minutes")  # type: ignore[assignment]
    return out


def _r(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)


def summarise(samples: list[dict[str, Any]], precip_total: float | None) -> dict[str, Any]:
    def col(k: str) -> list[float]:
        return [s[k] for s in samples if isinstance(s.get(k), (int, float))]

    t, dp, rh = col("temp_c"), col("dew_point_c"), col("rh_pct")
    wb, hi, wind, sun = col("wbgt_shade_est_c"), col("heat_index_c"), col("wind_kmh"), col("solar_wm2")
    def mean(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    s = {
        "temp_c": {"min": _r(min(t)) if t else None, "max": _r(max(t)) if t else None, "mean": _r(mean(t))},
        "dew_point_c": {"min": _r(min(dp)) if dp else None, "max": _r(max(dp)) if dp else None, "mean": _r(mean(dp))},
        "rh_pct": {"min": _r(min(rh), 0) if rh else None, "max": _r(max(rh), 0) if rh else None},
        "heat_index_c_max": _r(max(hi)) if hi else None,
        "wbgt_shade_est_c": {"mean": _r(mean(wb)), "max": _r(max(wb)) if wb else None},
        "wind_kmh_mean": _r(mean(wind)),
        "solar_wm2_max": _r(max(sun), 0) if sun else None,
        "precip_mm": _r(precip_total),
    }
    level = heat_level(max(wb) if wb else None)
    humid = bool(dp) and max(dp) >= HUMID_DEW_C
    strong_sun = bool(sun) and max(sun) >= STRONG_SUN_WM2
    s["heat_level"] = level
    s["humid"] = humid
    s["strong_sun"] = strong_sun
    parts = []
    if t:
        parts.append(f"氣溫 {s['temp_c']['min']}~{s['temp_c']['max']}°C")
    if dp:
        parts.append(f"露點 {s['dew_point_c']['min']}~{s['dew_point_c']['max']}°C")
    if wb:
        parts.append(f"WBGT(陰影估) 最高 {s['wbgt_shade_est_c']['max']}°C＝{level}")
    if humid:
        parts.append(f"露點 ≥{HUMID_DEW_C:.0f}°C＝濕熱")
    if strong_sun:
        parts.append(f"日照最高 {s['solar_wm2_max']:.0f} W/m²（陰影 WBGT 低估約 2~3°C）")
    if wind:
        parts.append(f"平均風速 {s['wind_kmh_mean']} km/h")
    if precip_total:
        parts.append(f"降雨 {s['precip_mm']} mm")
    s["verdict"] = "；".join(parts)
    # 校正判讀（PROC-93）：熱或濕熱時，掉速／漂移／心率偏高先歸環境，不判體能、不拿來改閾值
    s["correct_interpretation"] = level in ("熱", "高熱") or humid
    return s


# ---------------------------------------------------------------------------
# Heat-tolerance log (PROC-93 ③)
# ---------------------------------------------------------------------------
def learning_dir() -> Path | None:
    env = os.environ.get("TP_LO_LEARNING_DIR")
    if env:
        return Path(env).expanduser()
    default = Path.home() / "Claude" / "Projects" / "TP 訓練助理" / "tp-ai-layer" / "learning"
    return default if default.parent.exists() else None


def append_heat_log(record: dict[str, Any]) -> str | None:
    d = learning_dir()
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    path = d / "heat_log.jsonl"
    key = (str(record.get("athlete")), str(record.get("workout_id")))
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                old = json.loads(line)
            except ValueError:
                continue
            if (str(old.get("athlete")), str(old.get("workout_id"))) == key:
                return str(path)  # idempotent
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(path)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
async def _fetch(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    r = await client.get(url)
    r.raise_for_status()
    return r.json()


def _sample_from(row: dict[str, float | None], extra: dict[str, Any]) -> dict[str, Any]:
    t, rh = row.get("temperature_2m"), row.get("relative_humidity_2m")
    wb = wbgt_shade_est(t, rh) if t is not None and rh is not None else None
    hi = heat_index_c(t, rh) if t is not None and rh is not None else None
    return {
        **extra,
        "local_time": row.get("_local_time"),
        "temp_c": _r(t), "rh_pct": _r(rh, 0), "dew_point_c": _r(row.get("dew_point_2m")),
        "apparent_c": _r(row.get("apparent_temperature")), "heat_index_c": _r(hi),
        "wbgt_shade_est_c": _r(wb), "wind_kmh": _r(row.get("wind_speed_10m")),
        "gust_kmh": _r(row.get("wind_gusts_10m")), "wind_dir_deg": _r(row.get("wind_direction_10m"), 0),
        "solar_wm2": _r(row.get("shortwave_radiation"), 0), "cloud_pct": _r(row.get("cloud_cover"), 0),
    }


async def lo_weather_history(
    workout_id: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    start: str | None = None,
    duration_min: float | None = None,
    n_points: int = 3,
    log: bool = True,
    athlete: str | None = None,
    today: dt.date | None = None,
    fetcher: Any = None,
) -> dict[str, Any]:
    """See module docstring. ``start`` (manual mode) is local wall-clock time
    'YYYY-MM-DDTHH:MM' at the given lat/lon. ``fetcher`` is for tests."""
    totals: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    targets: list[dict[str, Any]] = []      # {lat, lon, when_utc | when_local, offset_s}
    manual_local = False

    if workout_id:
        from tp_mcp.tools.analyze import tp_analyze_workout
        from tp_mcp.tools.workouts import tp_get_workout

        analysis = await tp_analyze_workout(str(workout_id))
        if analysis.get("isError"):
            return analysis
        raw: dict[str, Any] = {}
        if analysis.get("data_file"):
            try:
                with open(analysis["data_file"], encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                raw = {}
        start_utc = parse_utc(raw.get("startTimestamp") or analysis.get("startTimestamp"))
        stop_utc = parse_utc(raw.get("stopTimestamp"))
        totals = analysis.get("totals") or {}
        chans = analysis.get("channels_min_max_avg") or {}
        meta = {"workout_id": str(workout_id), "start_utc": start_utc.isoformat() if start_utc else None,
                "stop_utc": stop_utc.isoformat() if stop_utc else None}
        try:
            detail = await tp_get_workout(str(workout_id))
            if not detail.get("isError"):
                meta.update(date=detail.get("date"), sport=detail.get("sport"), title=detail.get("title"))
        except Exception:  # noqa: BLE001 — titles are cosmetic
            pass
        meta["avg_power"] = (chans.get("Power") or [None, None, None])[2]
        meta["avg_hr"] = (chans.get("HeartRate") or [None, None, None])[2]
        pts = [p for p in (raw.get("data") or []) if isinstance(p, dict)]
        # device temperature is reported for contrast only (sun + body heat)
        dev = [p["Temperature"] for p in pts if isinstance(p.get("Temperature"), (int, float))]
        if dev:
            meta["device_temp_c"] = {"mean": _r(sum(dev) / len(dev)), "max": _r(max(dev)),
                                     "note": "碼錶溫度＝受日曬與體溫影響，只作對照，判讀以氣象值為準"}
        if start_utc is None:
            return _err("NO_START_TIME", "Workout has no start timestamp (manual entry?).", **meta)
        if lat is not None and lon is not None:
            span = (stop_utc - start_utc).total_seconds() if stop_utc else 0
            targets = [{"lat": float(lat), "lon": float(lon), "offset_s": o}
                       for o in ([0.0, span / 2, span] if span > 0 else [0.0])]
            meta["location_source"] = "caller"
        else:
            targets = sample_track(pts, n_points)
            meta["location_source"] = "gps"
            if not targets:
                return {"success": True, "outdoor": False, **meta,
                        "message": "No GPS in this activity (indoor / Zwift / treadmill / pool). "
                                   "Weather not applicable; pass lat/lon to force a location."}
        for tg in targets:
            tg["when_utc"] = start_utc + dt.timedelta(seconds=tg["offset_s"])
    else:
        if lat is None or lon is None or not start:
            return _err("INVALID_ARGS", "Give workout_id, or lat + lon + start ('YYYY-MM-DDTHH:MM' local).")
        try:
            local0 = dt.datetime.fromisoformat(start)
        except ValueError:
            return _err("INVALID_ARGS", "start must be 'YYYY-MM-DDTHH:MM' (local time at the location).")
        span = float(duration_min or 0) * 60
        targets = [{"lat": float(lat), "lon": float(lon), "offset_s": o, "when_local": local0 + dt.timedelta(seconds=o)}
                   for o in ([0.0, span / 2, span] if span > 0 else [0.0])]
        manual_local = True
        meta = {"location_source": "caller", "start_local": local0.isoformat(timespec="minutes")}

    # one request per (rounded) location and day
    def day_of(tg: dict[str, Any]) -> dt.date:
        return (tg["when_local"] if manual_local else tg["when_utc"]).date()

    urls: dict[tuple[float, float, dt.date], str] = {}
    for tg in targets:
        key = (round(tg["lat"], 2), round(tg["lon"], 2), day_of(tg))
        tg["key"] = key
        if key not in urls:
            d0 = key[2] - dt.timedelta(days=1)          # local day may start the previous UTC day
            urls[key] = build_url(key[0], key[1], d0, pick_endpoint(key[2], today), today)

    responses: dict[tuple[float, float, dt.date], dict[str, Any]] = {}
    try:
        if fetcher is not None:
            for k, u in urls.items():
                responses[k] = await fetcher(u)
        else:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                for k, u in urls.items():
                    responses[k] = await _fetch(client, u)
    except (httpx.HTTPError, ValueError) as e:
        return _err("WEATHER_FETCH_FAILED",
                    f"Open-Meteo request failed from this machine ({type(e).__name__}: {e}). "
                    "Fetch fallback_urls another way (e.g. Firecrawl) — same numbers.",
                    fallback_urls=sorted(set(urls.values())), **meta)

    samples: list[dict[str, Any]] = []
    precip_seen: dict[tuple[Any, str], float] = {}
    for tg in targets:
        resp = responses.get(tg["key"]) or {}
        hourly = resp.get("hourly") or {}
        off = int(resp.get("utc_offset_seconds") or 0)
        when_utc = tg.get("when_utc") or (tg["when_local"] - dt.timedelta(seconds=off)).replace(tzinfo=dt.timezone.utc)
        row = interpolate(hourly, off, when_utc)
        samples.append(_sample_from(row, {"offset_min": round(tg["offset_s"] / 60), "lat": round(tg["lat"], 4),
                                          "lon": round(tg["lon"], 4)}))
        # precipitation: hourly totals of the hours the session touched (per location)
        times, pr = hourly.get("time") or [], hourly.get("precipitation") or []
        hour = (when_utc + dt.timedelta(seconds=off)).replace(tzinfo=None, minute=0, second=0, microsecond=0)
        for i, ts in enumerate(times):
            if i < len(pr) and pr[i] is not None and dt.datetime.fromisoformat(ts) == hour:
                precip_seen[(tg["key"], ts)] = float(pr[i])
    precip_total = sum(precip_seen.values()) if precip_seen else None
    summary = summarise(samples, precip_total)
    any_resp = next(iter(responses.values()), {})
    out: dict[str, Any] = {
        "success": True, "outdoor": True, **meta,
        "source": "Open-Meteo " + ("+".join(sorted({u.split('//')[1].split('/')[0] for u in urls.values()}))),
        "timezone": any_resp.get("timezone"),
        "summary": summary, "samples": samples,
    }

    if log and workout_id and athlete:
        rec = {
            "athlete": str(athlete), "workout_id": str(workout_id), "date": meta.get("date"),
            "sport": meta.get("sport"), "title": meta.get("title"),
            "duration_s": totals.get("Duration"), "ef": totals.get("EF"),
            "pw_hr": totals.get("Pw:Hr") if totals.get("Pw:Hr") is not None else totals.get("Pa:Hr"),
            "np": totals.get("NP"), "avg_power": meta.get("avg_power"), "avg_hr": meta.get("avg_hr"),
            "temp_mean": summary["temp_c"]["mean"], "dew_mean": summary["dew_point_c"]["mean"],
            "dew_max": summary["dew_point_c"]["max"], "wbgt_mean": summary["wbgt_shade_est_c"]["mean"],
            "wbgt_max": summary["wbgt_shade_est_c"]["max"], "heat_level": summary["heat_level"],
            "solar_max": summary["solar_wm2_max"],
        }
        out["heat_log"] = append_heat_log(rec)
    return out


def register_lo_weather(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool  # local import: keep module importable in tests

    tools.append(Tool(
        name="lo_weather_history",
        description=(
            "Historical weather during a past session or race (PROC-93). workout_id → reads the "
            "activity's UTC start and GPS track, samples up to n_points positions at their real "
            "times and returns Open-Meteo hourly history interpolated to each: air temp, RH, dew "
            "point, heat index, shade-WBGT estimate, wind, solar radiation, rain; summary.heat_level "
            "(涼爽/溫和/熱/高熱 on WBGT 18/23/28), humid (dew ≥20 °C), strong_sun, verdict line and "
            "correct_interpretation (true ⇒ fades/drift/high HR are environmental first, never used to "
            "change thresholds). No GPS ⇒ outdoor:false. Manual mode: lat+lon+start (local "
            "'YYYY-MM-DDTHH:MM') + duration_min. Appends EF/Pw:Hr vs dew point to "
            "learning/heat_log.jsonl (log=false to skip). On network failure returns fallback_urls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workout_id": {"type": "string"},
                "lat": {"type": "number", "description": "Override / manual location."},
                "lon": {"type": "number"},
                "start": {"type": "string", "description": "Manual mode: local start 'YYYY-MM-DDTHH:MM'."},
                "duration_min": {"type": "number", "description": "Manual mode: session length."},
                "n_points": {"type": "integer", "default": 3,
                             "description": "GPS positions sampled along the track (1–6)."},
                "log": {"type": "boolean", "default": True},
            },
        },
    ))

    async def _h(args: dict[str, Any]) -> dict[str, Any]:
        from tp_mcp.client.context import athlete_override

        return await lo_weather_history(
            workout_id=args.get("workout_id"), lat=args.get("lat"), lon=args.get("lon"),
            start=args.get("start"), duration_min=args.get("duration_min"),
            n_points=max(1, min(6, int(args.get("n_points", 3)))), log=bool(args.get("log", True)),
            athlete=args.get("athlete") or athlete_override.get(),
        )

    handlers["lo_weather_history"] = _h
