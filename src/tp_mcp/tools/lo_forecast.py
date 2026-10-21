"""羅教練 fork-only: ``lo_weather_forecast`` — next week's weather, per athlete, as decisions.

**Context (PROC-85b, 2026/10/21 教練裁示「全套：工具＋決策表＋全員必跑」).** Weekly
planning looked at the forecast only for long-session days, race weeks and travel weeks
(PROC-85 ⑥), by hand, with no record. Run quality sessions — almost all outdoors — never
saw the forecast, so a threshold set written for 18 °C was run at WBGT 27 with no
correction, and rain / gale / typhoon / smog were handled only if someone happened to
look.

What it does, in one call:

1. Resolves each athlete to a training location from
   ``tp-ai-layer/memory/system/training-locations.json`` (slug, name or alias), or takes
   ``lat``/``lon`` or a ``place`` name (Open-Meteo geocoding). Athletes sharing a city
   share one request.
2. Pulls Open-Meteo hourly forecast (temp, RH, dew point, rain probability and amount,
   wind and gusts, solar) and the air-quality forecast (US AQI, PM2.5, ~5 days).
3. Per day: daytime (06–20 h local) max shade-WBGT estimate and heat level (same cut
   points as PROC-93: 18/23/28), dew point, the coolest dry 2-hour training window, rain,
   gusts, AQI, cold, a forecast-confidence grade by lead time, and the PROC-85b decision
   table's actions for run quality / long sessions / outdoor bike.

The decision table lives in ``ACTIONS`` below and in method/9 §9.7 — change both
together. The tool only proposes; the schedule is decided in Step 2.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from tp_mcp.tools.lo_weather import HUMID_DEW_C, STRONG_SUN_WM2, heat_level, wbgt_shade_est

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
HOURLY = ("temperature_2m", "relative_humidity_2m", "dew_point_2m", "precipitation_probability",
          "precipitation", "wind_speed_10m", "wind_gusts_10m", "shortwave_radiation")
TIMEOUT_S = 20.0
DAY_HOURS = range(6, 21)          # training day 06:00–20:59 (PER-50: no session before 06:00)
WINDOW_H = 2

# ---- PROC-85b thresholds (method/9 §9.7 — keep in step) ---------------------
RAIN_PROB = 70                    # % in the hour, with
RAIN_MM_DAY = 5.0                 # daytime total mm  → rain day
GUST_BIKE = 50.0                  # km/h → outdoor bike moves indoors
GUST_STORM = 62.0                 # km/h (Beaufort 8) → all outdoor moves; check official warnings
AQI_SENSITIVE = 101               # US AQI → outdoor quality / long sessions indoors or moved
AQI_UNHEALTHY = 151               # → every outdoor session indoors
COLD_C = 0.0                      # daytime min → speed work indoors / longer warm-up
SEVERE_COLD_C = -10.0             # → outdoor sessions indoors
# lead-time confidence: day 0–2 高, 3–4 中, ≥5 低 (低 = schedule only, never the description)
CONF = ((2, "高"), (4, "中"))

ACTIONS = {
    "熱": {
        "run_quality": "排當天最涼時段；閾值／節奏／比賽強度段配速放寬 2–4% 並加心率上限；"
                       "單趟 ≤3 分的速度間歇配速不放、組間 +30 秒",
        "long": "長跑／長騎挪到本週較涼的一天或最涼時段開始；補給水量照 FMT-37 上調",
        "bike_quality": "排最涼時段；功率照處方，心率超過閾值心率就降到處方下緣",
    },
    "高熱": {
        "run_quality": "改跑步機（室內）或挪日；都不行＝改心率控制版（主段以閾值心率 −5 為上限）",
        "long": "挪日；挪不了＝長跑縮短 20–30%、長騎改室內",
        "bike_quality": "改室內騎台",
    },
    "rain": {
        "bike": "戶外騎車改室內騎台",
        "run_quality": "速度間歇改跑步機或挪日（濕滑）；閾值／有氧跑可照跑",
    },
    "gust": {"bike": "陣風 ≥50 km/h：戶外騎車改室內"},
    "storm": {"all": "陣風 ≥62 km/h：所有戶外課改室內或挪日，並查官方颱風／強風警報"},
    "aqi_sensitive": {"all": "AQI 101–150：戶外質量課與長課改室內或挪日（呼吸道敏感者全部戶外課）"},
    "aqi_unhealthy": {"all": "AQI ≥151：所有戶外課改室內"},
    "cold": {"run_quality": "低溫 ≤0°C：速度間歇改室內或暖身加長 10 分鐘"},
    "severe_cold": {"all": "低溫 ≤−10°C：戶外課改室內"},
}


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message, **extra}


def locations_path() -> Path:
    env = os.environ.get("TP_LO_LOCATIONS")
    if env:
        return Path(env).expanduser()
    return (Path.home() / "Claude" / "Projects" / "TP 訓練助理" / "tp-ai-layer" / "memory" / "system"
            / "training-locations.json")


def load_locations(path: Path | None = None) -> dict[str, dict[str, Any]]:
    p = path or locations_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in (data.get("athletes") or {}).items() if isinstance(v, dict)}


def resolve(key: str, table: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    k = key.strip().lower()
    for slug, rec in table.items():
        names = [slug, str(rec.get("name") or ""), str(rec.get("tp_id") or "")] + list(rec.get("aliases") or [])
        if any(k == str(n).strip().lower() for n in names if n):
            return slug, rec
    return None


def confidence(lead_days: int) -> str:
    for cut, name in CONF:
        if lead_days <= cut:
            return name
    return "低"


def forecast_url(lat: float, lon: float, days: int) -> str:
    return (f"{FORECAST_URL}?latitude={lat:.4f}&longitude={lon:.4f}&hourly={','.join(HOURLY)}"
            f"&timezone=auto&forecast_days={max(1, min(16, days))}")


def air_url(lat: float, lon: float, days: int) -> str:
    return (f"{AIR_URL}?latitude={lat:.4f}&longitude={lon:.4f}&hourly=us_aqi,pm2_5"
            f"&timezone=auto&forecast_days={max(1, min(7, days))}")


def geo_url(place: str) -> str:
    return f"{GEO_URL}?name={urllib.parse.quote(place)}&count=1&language=zh&format=json"


def _r(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)


def best_window(hours: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Coolest WBGT 2-hour slot inside DAY_HOURS, preferring dry slots (rain prob < RAIN_PROB)."""
    best = None
    by_h = {h["hour"]: h for h in hours}
    for start in DAY_HOURS:
        slot = [by_h.get(start + i) for i in range(WINDOW_H)]
        if any(s is None or s.get("wbgt") is None for s in slot) or start + WINDOW_H - 1 > max(DAY_HOURS):
            continue
        wet = any((s.get("rain_prob") or 0) >= RAIN_PROB for s in slot)
        score = (wet, sum(s["wbgt"] for s in slot) / WINDOW_H)
        if best is None or score < best[0]:
            best = (score, start, slot)
    if best is None:
        return None
    (wet, mean), start, _slot = best
    return {"from": f"{start:02d}:00", "to": f"{start + WINDOW_H:02d}:00", "wbgt_mean": _r(mean), "wet": wet}


def assess_day(day: str, hours: list[dict[str, Any]], aqi: list[float], lead: int) -> dict[str, Any]:
    dh = [h for h in hours if h["hour"] in DAY_HOURS]
    def col(k: str) -> list[float]:
        return [h[k] for h in dh if isinstance(h.get(k), (int, float))]
    t, dp, wb = col("temp"), col("dew"), col("wbgt")
    rain_mm = sum(col("rain_mm")) if col("rain_mm") else 0.0
    rain_p = max(col("rain_prob")) if col("rain_prob") else None
    gust = max(col("gust")) if col("gust") else None
    sun = max(col("solar")) if col("solar") else None
    wbgt_max = max(wb) if wb else None
    level = heat_level(wbgt_max)
    humid = bool(dp) and max(dp) >= HUMID_DEW_C
    aqi_max = max(aqi) if aqi else None
    win = best_window(dh)

    flags: list[str] = []
    actions: dict[str, list[str]] = {"run_quality": [], "long": [], "bike": [], "all": []}
    heat_key = "高熱" if level == "高熱" else ("熱" if (level == "熱" or humid) else None)
    if heat_key:
        flags.append(f"{heat_key}（WBGT 最高 {_r(wbgt_max)}°C{'、露點 ≥20 濕熱' if humid else ''}）")
        a = ACTIONS[heat_key]
        actions["run_quality"].append(a["run_quality"])
        actions["long"].append(a["long"])
        actions["bike"].append(a["bike_quality"])
    if rain_p is not None and rain_p >= RAIN_PROB and rain_mm >= RAIN_MM_DAY:
        flags.append(f"雨（機率 {rain_p:.0f}%、白天 {rain_mm:.0f} mm）")
        actions["bike"].append(ACTIONS["rain"]["bike"])
        actions["run_quality"].append(ACTIONS["rain"]["run_quality"])
    if gust is not None and gust >= GUST_STORM:
        flags.append(f"強風（陣風 {gust:.0f} km/h）")
        actions["all"].append(ACTIONS["storm"]["all"])
    elif gust is not None and gust >= GUST_BIKE:
        flags.append(f"陣風 {gust:.0f} km/h")
        actions["bike"].append(ACTIONS["gust"]["bike"])
    if aqi_max is not None and aqi_max >= AQI_UNHEALTHY:
        flags.append(f"空汙（AQI {aqi_max:.0f}）")
        actions["all"].append(ACTIONS["aqi_unhealthy"]["all"])
    elif aqi_max is not None and aqi_max >= AQI_SENSITIVE:
        flags.append(f"空汙偏高（AQI {aqi_max:.0f}）")
        actions["all"].append(ACTIONS["aqi_sensitive"]["all"])
    tmin = min(t) if t else None
    if tmin is not None and tmin <= SEVERE_COLD_C:
        flags.append(f"嚴寒（{tmin:.0f}°C）")
        actions["all"].append(ACTIONS["severe_cold"]["all"])
    elif tmin is not None and tmin <= COLD_C:
        flags.append(f"低溫（{tmin:.0f}°C）")
        actions["run_quality"].append(ACTIONS["cold"]["run_quality"])

    conf = confidence(lead)
    return {
        "date": day,
        "weekday": "一二三四五六日"[dt.date.fromisoformat(day).weekday()],
        "lead_days": lead, "confidence": conf,
        "temp_c": {"min": _r(tmin), "max": _r(max(t)) if t else None},
        "dew_max_c": _r(max(dp)) if dp else None,
        "wbgt_max_c": _r(wbgt_max), "heat_level": level, "humid": humid,
        "strong_sun": bool(sun) and sun >= STRONG_SUN_WM2,
        "rain_prob_max": rain_p, "rain_mm_day": _r(rain_mm), "gust_max_kmh": _r(gust, 0),
        "aqi_max": _r(aqi_max, 0) if aqi_max is not None else None,
        "best_window": win,
        "flags": flags,
        "actions": {k: v for k, v in actions.items() if v},
        "hit": bool(flags),
        "description_ok": bool(flags) and conf != "低",
    }


def build_days(fc: dict[str, Any], air: dict[str, Any] | None, today: dt.date) -> list[dict[str, Any]]:
    h = fc.get("hourly") or {}
    times = h.get("time") or []
    rows: dict[str, list[dict[str, Any]]] = {}
    for i, ts in enumerate(times):
        def g(k: str, i: int = i) -> Any:
            s = h.get(k) or []
            return s[i] if i < len(s) else None
        stamp = dt.datetime.fromisoformat(ts)
        t, rh = g("temperature_2m"), g("relative_humidity_2m")
        rows.setdefault(stamp.date().isoformat(), []).append({
            "hour": stamp.hour, "temp": t, "dew": g("dew_point_2m"),
            "wbgt": wbgt_shade_est(t, rh) if t is not None and rh is not None else None,
            "rain_prob": g("precipitation_probability"), "rain_mm": g("precipitation"),
            "gust": g("wind_gusts_10m"), "solar": g("shortwave_radiation"),
        })
    aqi_by_day: dict[str, list[float]] = {}
    ah = (air or {}).get("hourly") or {}
    for ts, v in zip(ah.get("time") or [], ah.get("us_aqi") or [], strict=False):
        stamp = dt.datetime.fromisoformat(ts)
        if v is not None and stamp.hour in DAY_HOURS:
            aqi_by_day.setdefault(stamp.date().isoformat(), []).append(float(v))
    return [assess_day(d, hrs, aqi_by_day.get(d, []), (dt.date.fromisoformat(d) - today).days)
            for d, hrs in sorted(rows.items())]


async def _get(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    r = await client.get(url)
    r.raise_for_status()
    return r.json()


def _line(days: list[dict[str, Any]]) -> str:
    parts = []
    for d in days:
        tag = "、".join(d["flags"]) if d["flags"] else "—"
        w = d.get("best_window") or {}
        parts.append(f"{d['date'][5:]}({d['weekday']}) WBGT{d['wbgt_max_c']} {tag}"
                     + (f" 涼窗{w.get('from')}" if d["flags"] and w else "") + f" [{d['confidence']}]")
    return "｜".join(parts)


async def lo_weather_forecast(
    athletes: list[str] | None = None,
    lat: float | None = None,
    lon: float | None = None,
    place: str | None = None,
    start: str | None = None,
    days: int = 7,
    today: dt.date | None = None,
    fetcher: Any = None,
    locations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``start`` = first day to report (YYYY-MM-DD, default today; e.g. next Monday).
    ``fetcher`` / ``locations`` / ``today`` are for tests."""
    today = today or dt.date.today()
    try:
        first = dt.date.fromisoformat(start) if start else today
    except ValueError:
        return _err("INVALID_ARGS", "start must be YYYY-MM-DD")
    if (first - today).days > 9:
        return _err("INVALID_ARGS", "start is more than 9 days out — forecast skill is too low to plan on.")
    span = (first - today).days + max(1, min(14, int(days)))

    targets: list[dict[str, Any]] = []      # {label, lat, lon, place, athletes}
    unresolved: list[str] = []
    if athletes:
        table = locations if locations is not None else load_locations()
        if not table:
            return _err("NO_LOCATIONS", f"training-locations.json not found or empty at {locations_path()}")
        for a in athletes:
            hit = resolve(str(a), table)
            if not hit or hit[1].get("lat") is None or hit[1].get("lon") is None:
                unresolved.append(str(a))
                continue
            slug, rec = hit
            key = (round(float(rec["lat"]), 2), round(float(rec["lon"]), 2))
            tg = next((t for t in targets if t["key"] == key), None)
            if tg is None:
                tg = {"key": key, "lat": float(rec["lat"]), "lon": float(rec["lon"]),
                      "place": rec.get("place"), "athletes": []}
                targets.append(tg)
            tg["athletes"].append(slug)
    elif lat is not None and lon is not None:
        targets.append({"key": (round(lat, 2), round(lon, 2)), "lat": float(lat), "lon": float(lon),
                        "place": place, "athletes": []})
    elif not place:
        return _err("INVALID_ARGS", "Give athletes, or lat + lon, or place.")

    fetched: dict[str, Any] = {}
    urls: list[str] = []
    try:
        async def fetch(u: str, client: Any = None) -> dict[str, Any]:
            urls.append(u)
            if fetcher is not None:
                return await fetcher(u)
            return await _get(client, u)

        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            if not targets and place:
                g = await fetch(geo_url(place), client)
                res = (g.get("results") or [None])[0]
                if not res:
                    return _err("PLACE_NOT_FOUND", f"Geocoding found nothing for '{place}'.")
                targets.append({"key": None, "lat": float(res["latitude"]), "lon": float(res["longitude"]),
                                "place": f"{res.get('name')}（{res.get('admin1') or ''} {res.get('country') or ''}）",
                                "athletes": []})
            for tg in targets:
                fc = await fetch(forecast_url(tg["lat"], tg["lon"], span), client)
                try:
                    air = await fetch(air_url(tg["lat"], tg["lon"], span), client)
                except (httpx.HTTPError, ValueError):
                    air = None              # AQI is optional; heat/rain still decide
                fetched[id(tg)] = (fc, air)
    except (httpx.HTTPError, ValueError) as e:
        return _err("WEATHER_FETCH_FAILED",
                    f"Open-Meteo request failed from this machine ({type(e).__name__}: {e}). "
                    "Fetch fallback_urls another way (e.g. Firecrawl) — same numbers.",
                    fallback_urls=sorted(set(urls)))

    out_locs = []
    for tg in targets:
        fc, air = fetched[id(tg)]
        all_days = build_days(fc, air, today)
        sel = [d for d in all_days if first.isoformat() <= d["date"]][: max(1, min(14, int(days)))]
        out_locs.append({
            "place": tg.get("place"), "lat": round(tg["lat"], 3), "lon": round(tg["lon"], 3),
            "timezone": fc.get("timezone"), "athletes": tg["athletes"],
            "aqi_available": bool(air and (air.get("hourly") or {}).get("us_aqi")),
            "hit_days": [d["date"] for d in sel if d["hit"]],
            "line": _line(sel), "days": sel,
        })
    return {
        "success": True, "today": today.isoformat(), "start": first.isoformat(),
        "locations": out_locs,
        **({"unresolved": unresolved,
            "unresolved_note": "這些選手在 training-locations.json 沒有座標——以選單問教練訓練地後補檔（PROC-85b）"}
           if unresolved else {}),
        "note": ("PROC-85b：命中（hit）＝Step 2 照 actions 調排程；confidence 低（≥5 天後）只做排程判斷、"
                 "不寫說明段（description_ok=false），週檢再查。WBGT 為陰影估值，strong_sun 時實際高 2~3°C。"
                 "決策表正本＝method/9 §9.7。"),
    }


def register_lo_forecast(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(Tool(
        name="lo_weather_forecast",
        description=(
            "Weather forecast for planning (PROC-85b): per athlete training location (from "
            "training-locations.json by slug/name/alias/TP id), or lat+lon, or a place name. Per day: "
            "daytime max shade-WBGT and heat level (18/23/28), dew point, coolest dry 2-hour window "
            "(06–20 h), rain probability/amount, gusts, US AQI (~5 days), cold, lead-time confidence "
            "(高/中/低), flags and the PROC-85b decision-table actions for run quality / long / bike "
            "sessions. Athletes in the same city share one request. start=first day (e.g. next Monday). "
            "Read-only. On network failure returns fallback_urls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "athletes": {"type": "array", "items": {"type": "string"},
                             "description": "Slugs / names / TP ids in training-locations.json."},
                "lat": {"type": "number"}, "lon": {"type": "number"},
                "place": {"type": "string", "description": "Place name (geocoded) when no athlete/lat-lon."},
                "start": {"type": "string", "description": "YYYY-MM-DD first day to report (≤9 days ahead)."},
                "days": {"type": "integer", "default": 7},
            },
        },
    ))

    async def _h(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_weather_forecast(
            athletes=list(args.get("athletes") or []) or None, lat=args.get("lat"), lon=args.get("lon"),
            place=args.get("place"), start=args.get("start"), days=int(args.get("days", 7)),
        )

    handlers["lo_weather_forecast"] = _h
