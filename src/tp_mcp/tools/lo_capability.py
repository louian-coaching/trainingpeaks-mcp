"""羅教練 fork-only: ``lo_capability_scan`` — PROC-84 ② + PROC-85 ④⑤ in one call.

**Context (2026/10/01, sop-weekly Step 1 8f/8g).** Every week, for every athlete:
take the longest steady Z2 ride (≥90 min) / run (≥60 min) of the last two weeks,
read its aerobic decoupling, compare EF with the previous four weeks of the same
kind of session, sweep easy sessions for grey-zone drift, and — when EF jumps —
check power peaks against FTP. Done by hand that is one ``tp_analyze_workout``
per candidate plus arithmetic, and the arithmetic was being done on the charts
endpoint's ~12 s samples, which ``80b1d6d`` showed run 37 % low on power.

TrainingPeaks already computes the numbers on the full-resolution file: the
analysis *summary* carries ``Pw:Hr`` / ``Pa:Hr`` (decoupling, %), ``EF``, ``IF``,
``VI``, elapsed vs moving time. This tool reads only that summary (one request
per session, no time series), applies the 8f/8g thresholds the same way for
every athlete, and returns one row per athlete with flags and a level.

What it cannot see, and says so: heat (no temperature in the summary — 8f says
exclude sessions averaging >30 °C, so a red decoupling in a hot week is a
question, not a verdict), and threshold freshness (8f ③ lives in
``athlete_card.py --threshold-dates``, dated from the cards, not TP).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any

from tp_mcp.client.context import athlete_override
from tp_mcp.tools import lo_capability_ext as ext

logger = logging.getLogger(__name__)

# method/1 §三 titles. Steady = no sprints, no heat protocol (heat inflates HR).
_STEADY = {"Bike": ("有氧耐力", "長距離騎乘"), "Run": ("有氧耐力", "長跑")}
_EASY = {"Bike": ("有氧耐力", "長距離騎乘", "輕鬆騎"), "Run": ("有氧耐力", "長跑", "輕鬆跑")}
_EXCLUDE_WORDS = ("熱訓練", "間歇", "節奏", "比賽強度", "測驗", "Ramp", "力量", "變速", "漸速", "轉換", "換項")
_MIN_LONG_MIN = {"Bike": 90, "Run": 60}
_PAUSE_MAX_S = 300
_DECOUPLE_KEY = {"Bike": "Pw:Hr", "Run": "Pa:Hr"}


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _base_title(title: str | None) -> str:
    return (title or "").split("－")[0].strip()


def classify(sport: str, title: str | None) -> str | None:
    """'steady' | 'easy' | None for a completed session's title."""
    t = title or ""
    if sport not in _STEADY or any(w in t for w in _EXCLUDE_WORDS):
        return None
    base = _base_title(t)
    if any(base.startswith(s) for s in _STEADY[sport]):
        return "steady"
    if any(base.startswith(s) for s in _EASY[sport]):
        return "easy"
    return None


def decoupling_verdict(latest: float | None, previous: float | None) -> str | None:
    if latest is None:
        return None
    if latest <= 5:
        if previous is not None and previous <= 5:
            return "≤5% 連兩次：長課可加長或後段嵌比賽強度"
        return "≤5%：再看一週（連兩週才加長）"
    if latest <= 8:
        return "5–8%：維持長度"
    return ">8%：不加長，先查補給／氣溫／疲勞"


async def summary_totals(workout_id: str) -> dict[str, Any]:
    """TP's analysis summary only (no charts/laps): {friendlyName: value}."""
    import httpx

    from tp_mcp.client import TPClient
    from tp_mcp.tools.analyze import _SUMMARY_PATH, ANALYSIS_TIMEOUT, _post_analysis

    async with TPClient() as client:
        if not await client.ensure_athlete_id():
            return {"isError": True, "error_code": "AUTH_INVALID", "message": "no athlete id"}
        tok = await client._ensure_access_token()
        if not tok.success or not client._token_cache.access_token:
            return {"isError": True, "error_code": "AUTH_INVALID", "message": tok.message or "no token"}
        headers = {
            "Authorization": f"Bearer {client._token_cache.access_token}",
            "Accept": "application/json", "Content-Type": "application/json",
            "Origin": "https://app.trainingpeaks.com", "Referer": "https://app.trainingpeaks.com/",
        }
        async with httpx.AsyncClient(timeout=ANALYSIS_TIMEOUT) as http:
            body, err = await _post_analysis(http, _SUMMARY_PATH, headers, int(workout_id))
    if err:
        return err
    data = (body or {}).get("data") or {}
    out = {(m.get("friendlyName") or k): m.get("value") for k, m in data.items() if isinstance(m, dict)}
    out["_start"] = (body or {}).get("startTimestamp")      # UTC, for PER-79b weather (11/02)
    return out


async def session_max_hr(workout_id: str) -> float | None:
    """PROC-84g gate 1: the session's peak HR from TP's lap table (one light request,
    no time series)."""
    import httpx

    from tp_mcp.client import TPClient
    from tp_mcp.tools.analyze import _LAPS_PATH, ANALYSIS_TIMEOUT, _post_analysis

    async with TPClient() as client:
        if not await client.ensure_athlete_id():
            return None
        tok = await client._ensure_access_token()
        if not tok.success or not client._token_cache.access_token:
            return None
        headers = {
            "Authorization": f"Bearer {client._token_cache.access_token}",
            "Accept": "application/json", "Content-Type": "application/json",
            "Origin": "https://app.trainingpeaks.com", "Referer": "https://app.trainingpeaks.com/",
        }
        async with httpx.AsyncClient(timeout=ANALYSIS_TIMEOUT) as http:
            body, err = await _post_analysis(http, _LAPS_PATH, headers, int(workout_id))
    if err:
        return None
    vals = [_num(lap.get("MaximumHeartRate")) for lap in (body or {}).get("data") or [] if isinstance(lap, dict)]
    vals = [v for v in vals if v and v > 0]
    return max(vals) if vals else None


async def workout_rpe(workout_id: str) -> float | None:
    """PROC-84g gate 2: TP's RPE field of a completed workout (None when not filled)."""
    from tp_mcp.tools.workouts import tp_get_workout

    d = await tp_get_workout(str(workout_id))
    return None if not isinstance(d, dict) or d.get("isError") else _num(d.get("rpe"))


async def mean_temp_c(workout_id: str, athlete: str | None) -> float | None:
    """PROC-85g: mean air temperature of the session that set a power peak (None = indoor / unknown)."""
    from tp_mcp.tools.lo_weather import lo_weather_history

    try:
        r = await lo_weather_history(str(workout_id), athlete=athlete, log=False)
    except Exception:  # noqa: BLE001 — a weather failure must not cost the scan
        return None
    if not isinstance(r, dict) or r.get("isError") or not r.get("outdoor"):
        return None
    return _num(((r.get("summary") or {}).get("temp_c") or {}).get("mean"))


async def wellness_daily(start: str, end: str) -> list[dict[str, Any]]:
    """PER-79c: TP daily metrics (resting HR = Pulse, HRV) — empty when the athlete syncs none."""
    from tp_mcp.tools.metrics import tp_get_metrics

    r = await tp_get_metrics(start_date=start, end_date=end)
    return [] if not isinstance(r, dict) or r.get("isError") else list(r.get("daily") or [])


async def easy_run_weather(athlete_keys: list[str], runs: list[dict[str, Any]]) -> dict[str, str]:
    """PER-79b: {workout_id: why excluded} for hot (WBGT ≥23) / cold (≤5 °C) easy runs, from the
    athlete's training location (training-locations.json) at the run's mid-time. Runs with no
    start time or athletes with no coordinates are left in (and said so by the caller)."""
    import httpx

    from tp_mcp.tools import lo_weather as lw
    from tp_mcp.tools.lo_forecast import load_locations, resolve

    table = load_locations()
    loc = None
    for k in athlete_keys:
        if k and (hit := resolve(str(k), table)):
            loc = hit[1]
            break
    lat, lon = _num((loc or {}).get("lat")), _num((loc or {}).get("lon"))
    if lat is None or lon is None:
        return {}
    out: dict[str, str] = {}
    cache: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=lw.TIMEOUT_S) as http:
        for r in runs:
            st = lw.parse_utc(r.get("start_utc"))
            if not st:
                continue
            mid = st + dt.timedelta(minutes=(r.get("duration_min") or 0) / 2)
            day = mid.date()
            key = day.isoformat()
            try:
                if key not in cache:
                    cache[key] = await lw._fetch(http, lw.build_url(lat, lon, day, lw.pick_endpoint(day)))
                resp = cache[key]
                row = lw.interpolate(resp.get("hourly") or {}, int(resp.get("utc_offset_seconds") or 0), mid)
            except Exception:  # noqa: BLE001
                continue
            t, rh = row.get("temperature_2m"), row.get("relative_humidity_2m")
            if t is None or rh is None:
                continue
            wb = lw.wbgt_shade_est(t, rh)
            if lw.heat_level(wb) in ("熱", "高熱"):
                out[str(r.get("workout_id"))] = f"熱（WBGT 約 {wb:.0f}）"
            elif t <= lw.COLD_MEAN_C:
                out[str(r.get("workout_id"))] = f"冷（{t:.0f}°C）"
    return out


def _session_row(w: dict[str, Any], totals: dict[str, Any]) -> dict[str, Any]:
    sport = w.get("sport")
    elapsed, moving = _num(totals.get("Elapsed time")), _num(totals.get("Moving time"))
    dist_km = _num(totals.get("Distance"))
    row = {
        "workout_id": str(w.get("id")), "date": str(w.get("date"))[:10], "title": w.get("title"),
        "duration_min": round((moving or elapsed or 0) / 60, 1) if (moving or elapsed) else None,
        "decoupling_pct": _num(totals.get(_DECOUPLE_KEY.get(sport, ""))),
        "ef": _num(totals.get("EF")),
        "if": _num(totals.get("IF")) or _num(totals.get("rIF")),
        "vi": _num(totals.get("VI")),
        "pause_s": round(elapsed - moving) if elapsed and moving else None,
        "avg_hr": _num(totals.get("Avg heart rate")),
        "np": _num(totals.get("NP")),
        "gain_m": _num(totals.get("El. Gain")),
        "distance_km": dist_km,
        "start_utc": totals.get("_start"),
    }
    if sport == "Run" and dist_km and moving:
        row["avg_speed_ms"] = round(dist_km * 1000 / moving, 3)
    temp = next((v for k, v in totals.items() if "temp" in str(k).lower()), None)
    if temp is not None:
        row["temp_avg"] = _num(temp)
    return row


def _excluded_reason(row: dict[str, Any], sport: str, heat_c: float) -> str | None:
    if row.get("decoupling_pct") is None:
        return f"沒有 {_DECOUPLE_KEY[sport]}（缺心率或功率／配速）"
    if (row.get("pause_s") or 0) > _PAUSE_MAX_S:
        return f"中途停頓 {round(row['pause_s'] / 60)} 分（>5 分不納入）"
    if row.get("temp_avg") is not None and row["temp_avg"] > heat_c:
        return f"均溫 {row['temp_avg']:g}°C（>{heat_c:g}°C 不納入）"
    if (row.get("duration_min") or 0) < _MIN_LONG_MIN[sport]:
        return f"{row.get('duration_min')} 分，未達 {_MIN_LONG_MIN[sport]} 分"
    return None


def assess_sport(sport: str, rows: list[dict[str, Any]], window_start: str, heat_c: float = 30.0,
                 ef_up_pct: float = 3.0, hot_ids: set[str] | None = None) -> dict[str, Any]:
    """rows: steady sessions of one sport, newest first, with summary metrics.
    hot_ids: sessions PROC-93 marked correct_interpretation (hot / humid) — out of the trend."""
    usable, excluded = [], []
    for r in rows:
        why = _excluded_reason(r, sport, heat_c)
        if not why and hot_ids and str(r.get("workout_id")) in hot_ids:
            why = "熱／濕熱（PROC-93）：不進趨勢、不算連兩週"
        (excluded if why else usable).append({**r, **({"excluded": why} if why else {})})
    out: dict[str, Any] = {"metric": _DECOUPLE_KEY[sport], "excluded": excluded}
    in_window = [r for r in usable if r["date"] >= window_start]
    if not in_window:
        out["note"] = f"近窗內沒有可用的 {sport} 長課（≥{_MIN_LONG_MIN[sport]} 分、穩態標題）"
        return out
    latest = max(in_window, key=lambda r: (r.get("duration_min") or 0))  # 8f: the LONGEST one
    older = [r for r in usable if r["date"] < latest["date"]]
    prev = older[0] if older else None
    out["latest"] = latest
    if prev:
        out["previous"] = {k: prev.get(k) for k in ("workout_id", "date", "title", "decoupling_pct", "ef")}
    # PROC-84h (11/02): the 加長判準 reads the front part (first 10 min out, stops out,
    # 前 2 小時 only) when the time series was segmented; TP's whole-file value otherwise.
    d_latest, basis = ext.effective_decoupling(latest)
    d_prev = ext.effective_decoupling(prev)[0] if prev else None
    out["decoupling_used"], out["decoupling_basis"] = d_latest, basis
    out["decoupling_verdict"] = decoupling_verdict(d_latest, d_prev)
    back = (latest.get("seg") or {}).get("back") or {}
    if back.get("durability_pct") is not None:
        prev_back = next(((r.get("seg") or {}).get("back") for r in older
                          if ((r.get("seg") or {}).get("back") or {}).get("durability_pct") is not None), None)
        pb = (prev_back or {}).get("durability_pct")
        out["durability"] = {"latest_pct": back["durability_pct"], "previous_pct": pb,
                             "weak": back["durability_pct"] > ext.DURABILITY_WEAK_PCT
                             and pb is not None and pb > ext.DURABILITY_WEAK_PCT}
    if back.get("drift_into_heavy"):
        out["drift_into_heavy"] = {"avg_hr": back.get("avg_hr"), "vt1_hr": back.get("vt1_hr")}
    base = [r["ef"] for r in older if r.get("ef")]
    if latest.get("ef") and len(base) >= 2:
        mean = sum(base) / len(base)
        out["ef_baseline"] = round(mean, 3)
        out["ef_change_pct"] = round(100 * (latest["ef"] / mean - 1), 1)
        out["ef_up"] = out["ef_change_pct"] > ef_up_pct
    else:
        out["ef_note"] = "前 4 週同類課不足 2 堂，EF 不比"
    return out


def grey_zone(bike_easy: list[dict[str, Any]], run_easy: list[dict[str, Any]],
              run_z2_upper_ms: float | None, bike_if_max: float = 0.78) -> dict[str, Any]:
    hits = []
    for r in bike_easy:
        if r.get("if") is not None and r["if"] > bike_if_max:
            hits.append({**{k: r.get(k) for k in ("workout_id", "date", "title")},
                         "sport": "Bike", "why": f"IF {r['if']:.2f} > {bike_if_max}"})
    for r in run_easy:
        sp = r.get("avg_speed_ms")
        if run_z2_upper_ms and sp and sp > run_z2_upper_ms:
            pace = 1000 / sp
            cap = 1000 / run_z2_upper_ms
            hits.append({**{k: r.get(k) for k in ("workout_id", "date", "title")}, "sport": "Run",
                         "why": f"均速 {int(pace // 60)}:{int(round(pace % 60)):02d}/km 快於 Z2 上緣 "
                                f"{int(cap // 60)}:{int(round(cap % 60)):02d}/km"})
    out = {"checked": len(bike_easy) + len(run_easy), "hits": hits, "flag": len(hits) >= 2}
    if run_easy and not run_z2_upper_ms:
        out["note"] = "讀不到跑步 Z2 上緣（speedZones），跑步灰區未判"
    return out


def _zone_threshold(settings: dict[str, Any], key: str, wtid: int) -> dict[str, Any] | None:
    for g in settings.get(key) or []:
        if isinstance(g, dict) and g.get("workoutTypeId") == wtid:
            return g
    return None


async def _peaks(ftp: float | None, athlete: str | None = None) -> dict[str, Any]:
    """PROC-85g (11/02): three tiers instead of ×1.03; peaks set in sessions averaging
    <5 °C or >30 °C are skipped; the 2nd-best 20-min peak from another session is the
    automatic second source (a 基準測驗 or a ≥12-min hard session is the manual one)."""
    from tp_mcp.tools.peaks import tp_get_peaks

    out: dict[str, Any] = {}
    res = await tp_get_peaks(sport="Bike", pr_type="power20min", days=90)
    recs = [r for r in (res or {}).get("records") or [] if _num(r.get("value"))]
    usable, skipped = [], []
    for r in recs[:4]:
        t = await mean_temp_c(str(r.get("workout_id")), athlete) if r.get("workout_id") else None
        if t is not None and (t < ext.PEAK_TEMP_LO_C or t > ext.PEAK_TEMP_HI_C):
            skipped.append({"w": _num(r.get("value")), "date": r.get("date"), "temp_c": round(t, 1)})
            continue
        usable.append(r)
    if skipped:
        out["skipped_by_temp"] = skipped
    if usable:
        best = usable[0]
        v = _num(best.get("value"))
        out["bike_20min_best"] = {"w": v, "date": best.get("date"), "workout_id": best.get("workout_id")}
        second = next((r for r in usable[1:] if str(r.get("workout_id")) != str(best.get("workout_id"))), None)
        if second:
            out["bike_20min_second"] = {"w": _num(second.get("value")), "date": second.get("date")}
        if v and ftp:
            tier = ext.ftp_tier(v, ftp, _num((second or {}).get("value")))
            out["ftp_tier"] = tier
            out["bike_20min_x095"] = tier.get("x095")
            out["ftp_maybe_low"] = bool(tier.get("report"))
    for pr in ("speed5K", "speed10K"):
        r = await tp_get_peaks(sport="Run", pr_type=pr, days=90)
        rr = (r or {}).get("records") or []
        if rr:
            out[f"run_{pr}_best"] = {"value": rr[0].get("value"), "date": rr[0].get("date")}
    if any(k.startswith("run_") for k in out):
        out["run_note"] = "跑步只列近 90 天最佳，對應閾值配速由教練判讀（8g ④，三級門檻同 PROC-85g）"
    return out



def _base(title: str | None) -> str:
    return _base_title(title)


async def _progress_layer(requested: str, ident: dict[str, Any], ws: list[dict[str, Any]],
                          picks: dict[str, list[dict[str, Any]]], metrics: Any, window_start: str, end: str,
                          *, ledger: bool, swim: bool, weather: str, max_swims: int,
                          segments: bool = True, zones: dict[str, tuple[Any, Any]] | None = None) -> dict[str, Any]:
    """PROC-84d ledger + PROC-93 weather on steady sessions + SWIM-91 swims + PROC-37b baselines.
    Each session is analysed once in its life: anything already in the ledger is reused."""
    from tp_mcp.tools import lo_progress as lp
    from tp_mcp.tools.analyze import tp_analyze_workout
    from tp_mcp.tools.lo_weather import weather_brief

    aid = str(ident.get("athlete_id") or requested)
    hist = lp.read_ledger(aid) if ledger else []
    by_id = {str(r.get("workout_id")): r for r in hist}
    new: list[dict[str, Any]] = []
    out: dict[str, Any] = {"_hot_ids": set(), "_segs": {}, "weather": [], "baselines": [], "swims": []}
    base_rec = {"athlete_id": aid, "athlete": str(ident.get("athlete_name") or requested)}

    async def analyse(wid: str) -> dict[str, Any] | None:
        a = await tp_analyze_workout(wid)
        return None if a.get("isError") else a

    def segment(an: dict[str, Any], sport: str) -> dict[str, Any]:
        vt1, z2 = (zones or {}).get(sport) or (None, None)
        return ext.segment_decoupling(lp._points(an), sport, vt1_hr=vt1, z2_upper=z2)

    # 1) steady sessions: weather + ledger
    for sport in ("Bike", "Run"):
        for w in picks.get(sport) or []:
            wid = str(w.get("id"))
            row = await metrics(w)
            old = by_id.get(wid)
            wx = (old or {}).get("weather")
            seg = (old or {}).get("seg")
            in_win = str(w.get("date"))[:10] >= window_start
            if old is None and in_win:
                an = await analyse(wid) if (weather == "auto" or segments) else None
                if segments and an:
                    seg = segment(an, sport)
                if weather == "auto" and an:
                    wb = await weather_brief(wid, an, athlete=requested)
                    wx = {k: wb.get(k) for k in ("outdoor", "heat_level", "cold_level", "humid", "wbgt_max", "dew_mean",
                                                   "correct_interpretation", "verdict") if k in wb}
                rec = {**base_rec, "workout_id": wid, "date": row.get("date"), "sport": sport, "kind": "steady",
                       "title": row.get("title"), "duration_min": row.get("duration_min"),
                       "decoupling_pct": row.get("decoupling_pct"), "ef": row.get("ef"), "if": row.get("if"),
                       "avg_speed_ms": row.get("avg_speed_ms"), "weather": wx, "seg": seg, "logged": end}
                new.append(rec)
                if wx:
                    out["weather"].append({"date": row.get("date"), "sport": sport, "title": row.get("title"),
                                           "verdict": wx.get("verdict"),
                                           "correct_interpretation": wx.get("correct_interpretation")})
            elif segments and in_win and seg is None:      # logged before PROC-84h (11/02)
                an = await analyse(wid)
                if an:
                    seg = segment(an, sport)
            if seg:
                out["_segs"][wid] = seg
            if (wx or {}).get("correct_interpretation"):
                out["_hot_ids"].add(wid)

    # 2) baselines (PROC-37b) anywhere in the history window
    for w in ws:
        sport = w.get("sport")
        if sport not in ("Bike", "Run", "Swim") or not _base(w.get("title")).startswith(lp.BASELINE_TITLE):
            continue
        wid = str(w.get("id"))
        if wid in by_id:
            continue
        an = await analyse(wid)
        if not an:
            continue
        m = lp.baseline_metrics(sport, an)
        wx = None
        if sport != "Swim" and weather == "auto":
            wb = await weather_brief(wid, an, athlete=requested)
            wx = {k: wb.get(k) for k in ("outdoor", "heat_level", "correct_interpretation", "verdict") if k in wb}
        prev = sorted([r for r in hist if r.get("kind") == "baseline" and r.get("sport") == sport
                       and str(r.get("date")) < str(w.get("date"))[:10]], key=lambda r: str(r.get("date")))
        p = prev[-1] if prev else None
        cmp = lp.compare_baseline(sport, m or {}, (p or {}).get("metrics"), wx, (p or {}).get("weather"))
        if p:
            cmp["vs"] = p.get("date")
        rec = {**base_rec, "workout_id": wid, "date": str(w.get("date"))[:10], "sport": sport, "kind": "baseline",
               "title": w.get("title"), "metrics": m, "weather": wx, "compare": cmp, "logged": end}
        new.append(rec)
        out["baselines"].append(rec)

    # 3) swims in the window (SWIM-91)
    if swim:
        swims = [w for w in ws if w.get("sport") == "Swim" and str(w.get("date"))[:10] >= window_start
                 and str(w.get("id")) not in by_id and not _base(w.get("title")).startswith(lp.BASELINE_TITLE)]
        for w in swims[:max_swims]:
            wid = str(w.get("id"))
            an = await analyse(wid)
            m = lp.swim_metrics(an) if an else None
            if not m:
                continue
            tb = _base(w.get("title"))
            same = [r for r in hist if r.get("kind") == "swim" and r.get("title_base") == tb
                    and str(r.get("date")) < str(w.get("date"))[:10]][-8:]
            cmp = lp.compare_swim(m, same)
            rec = {**base_rec, "workout_id": wid, "date": str(w.get("date"))[:10], "sport": "Swim", "kind": "swim",
                   "title": w.get("title"), "title_base": tb, "swim": m, "compare": cmp, "logged": end}
            new.append(rec)
            out["swims"].append(rec)

    if ledger and new:
        out["ledger"] = lp.append_ledger(new)
    out["logged"] = len(new) if ledger else 0
    return {k: v for k, v in out.items() if v or k in ("_hot_ids", "_segs")}


async def _one(requested: str, *, end: str, window_days: int, history_days: int, max_per_sport: int,
               heat_c: float, bike_if_max: float, ef_up_pct: float, peaks: str,
               save_dir: Path | None, ledger: bool = True, swim: bool = True,
               weather: str = "auto", max_swims: int = 6, segments: bool = True, gates: bool = True,
               easy_index: bool = True, wellness: bool = True,
               power_runners: list[str] | None = None) -> dict[str, Any]:
    from tp_mcp.tools.lo_review import athlete_identity
    from tp_mcp.tools.settings import tp_get_athlete_settings
    from tp_mcp.tools.workouts import tp_get_workouts

    window_start = (dt.date.fromisoformat(end) - dt.timedelta(days=window_days - 1)).isoformat()
    hist_start = (dt.date.fromisoformat(end) - dt.timedelta(days=history_days - 1)).isoformat()
    token = athlete_override.set(requested)
    try:
        ident = await athlete_identity()
        if not ident.get("athlete_name") and not ident.get("athlete_id"):
            return {"athlete": requested, "level": "error", "flags": ["身分解析失敗"]}
        listed = await tp_get_workouts(start_date=hist_start, end_date=end, workout_filter="completed")
        if listed.get("isError"):
            raise RuntimeError(listed.get("message"))
        st = await tp_get_athlete_settings()
        settings = st.get("settings") or {} if not st.get("isError") else {}

        ws = sorted(listed.get("workouts") or [], key=lambda w: str(w.get("date")), reverse=True)
        picks: dict[str, list[dict[str, Any]]] = {"Bike": [], "Run": []}
        easy_picks: dict[str, list[dict[str, Any]]] = {"Bike": [], "Run": []}
        for w in ws:
            sport = w.get("sport")
            kind = classify(sport or "", w.get("title"))
            if not kind:
                continue
            dur_min = (w.get("duration_actual") or 0) * 60
            if kind == "steady" and dur_min >= _MIN_LONG_MIN[sport] * 0.95 and len(picks[sport]) < max_per_sport:
                picks[sport].append(w)
            if str(w.get("date"))[:10] >= window_start:
                easy_picks[sport].append(w)

        cache: dict[str, dict[str, Any]] = {}

        async def metrics(w: dict[str, Any]) -> dict[str, Any]:
            wid = str(w.get("id"))
            if wid not in cache:
                tot = await summary_totals(wid)
                cache[wid] = _session_row(w, {} if tot.get("isError") else tot)
                if tot.get("isError"):
                    cache[wid]["summary_error"] = tot.get("message")
            return cache[wid]

        result: dict[str, Any] = {}
        zones: dict[str, tuple[Any, Any]] = {}
        for sport, zkey, wtid in (("Bike", "powerZones", 2), ("Run", "speedZones", 3)):
            lthr = _num((_zone_threshold(settings, "heartRateZones", wtid) or {}).get("threshold"))
            zs = ((_zone_threshold(settings, zkey, wtid) or (_zone_threshold(settings, zkey, 0) if sport == "Bike"
                                                             else None) or {}).get("zones") or [])
            z2u = _num(zs[1].get("maximum")) if len(zs) > 1 and isinstance(zs[1], dict) else None
            zones[sport] = (lthr * ext.VT1_FROM_LTHR if lthr else None, z2u)
        prog = await _progress_layer(requested, ident, ws, picks, metrics, window_start, end,
                                     ledger=ledger, swim=swim, weather=weather, max_swims=max_swims,
                                     segments=segments, zones=zones)
        hot_ids = prog.pop("_hot_ids", set())
        segs = prog.pop("_segs", {})
        for sport in ("Bike", "Run"):
            rows = [await metrics(w) for w in picks[sport]]
            for r in rows:
                if r.get("workout_id") in segs:
                    r["seg"] = segs[r["workout_id"]]
            result[sport.lower()] = assess_sport(sport, rows, window_start, heat_c, ef_up_pct, hot_ids=hot_ids)
            a = result[sport.lower()]
            if gates and a.get("ef_up"):
                try:
                    a["ef_gates"] = await _ef_gates(sport, ws, window_start, a, picks[sport], metrics)
                except Exception as e:  # noqa: BLE001
                    a["ef_gates"] = {"rule": "PROC-84g", "pass": None, "verdict": f"兩關檢查失敗（{e}），人工判"}
        if prog:
            result["progress"] = prog
        bike_easy = [await metrics(w) for w in easy_picks["Bike"]]
        run_easy = [await metrics(w) for w in easy_picks["Run"]]
        run_set = _zone_threshold(settings, "speedZones", 3) or {}
        zones = run_set.get("zones") or []
        z2 = _num(zones[1].get("maximum")) if len(zones) > 1 and isinstance(zones[1], dict) else None
        result["grey_zone"] = grey_zone(bike_easy, run_easy, z2, bike_if_max)
        bike_set = _zone_threshold(settings, "powerZones", 2) or _zone_threshold(settings, "powerZones", 0) or {}
        ftp = _num(bike_set.get("threshold"))
        result["thresholds"] = {"bike_ftp": ftp, "run_threshold_ms": _num(run_set.get("threshold")),
                                "run_z2_upper_ms": z2}
        # PROC-84g: an EF rise that failed a gate is fatigue, not a reason to look at peaks
        ef_up = any((result.get(s) or {}).get("ef_up") and ((result.get(s) or {}).get("ef_gates") or {}).get("pass")
                    is not False for s in ("bike", "run"))
        if peaks == "always" or (peaks == "auto" and ef_up):
            result["peaks"] = await _peaks(ftp, requested)
            result["peaks"]["trigger"] = "always" if peaks == "always" else "EF 升 >3%（PROC-84g 未擋）"
        keys = [requested, str(ident.get("athlete_name") or ""), str(ident.get("athlete_id") or "")]
        if easy_index:
            try:
                result["easy_run"] = await _easy_runs(ws, end, metrics, keys, power_runners or [])
            except Exception as e:  # noqa: BLE001
                result["easy_run"] = {"rule": "PER-79b", "verdict": f"計算失敗（{e}）", "abnormal": None}
        if wellness:
            try:
                start_w = (dt.date.fromisoformat(end) - dt.timedelta(days=34)).isoformat()
                result["wellness"] = ext.wellness_signal(await wellness_daily(start_w, end), end)
            except Exception as e:  # noqa: BLE001
                result["wellness"] = {"rule": "PER-79c", "flags": [], "note": f"讀取失敗（{e}）"}
    except Exception as e:  # noqa: BLE001
        logger.exception("capability scan failed for %s", requested)
        return {"athlete": requested, "level": "error", "flags": [f"讀取失敗：{e}"]}
    finally:
        athlete_override.reset(token)

    flags, level = [], "green"
    for s, label in (("bike", "騎"), ("run", "跑")):
        a = result.get(s) or {}
        d = a.get("decoupling_used", (a.get("latest") or {}).get("decoupling_pct"))
        if d is not None:
            if d > 8:
                flags.append(f"{label}漂移 {d:g}%>8")
                level = "red"
            elif d > 5:
                flags.append(f"{label}漂移 {d:g}%")
                level = "yellow" if level == "green" else level
        if a.get("ef_up"):
            g = (a.get("ef_gates") or {}).get("pass")
            if g is False:
                flags.append(f"{label} EF +{a['ef_change_pct']:g}%：疲勞型 EF 上升（PROC-84g，轉 PER-49）")
            else:
                gate_txt = "，兩關通過" if g else "，兩關待人工"
                flags.append(f"{label} EF +{a['ef_change_pct']:g}%（閾值可能偏低{gate_txt}）")
            level = "yellow" if level == "green" else level
        if (a.get("durability") or {}).get("weak"):
            flags.append(f"{label}耐久性弱項：2 小時後解耦連 2 次 >8%（PROC-84h）")
            level = "yellow" if level == "green" else level
        if a.get("drift_into_heavy"):
            flags.append(f"{label}長課 2 小時後漂入中強度（該段不算純 Z2）")
            level = "yellow" if level == "green" else level
    if (result.get("easy_run") or {}).get("abnormal"):
        flags.append("輕鬆跑同配速心率 >1 SD（PER-79b，S3 第 4 項）")
        level = "yellow" if level == "green" else level
    for f in (result.get("wellness") or {}).get("flags") or []:
        flags.append(f + "（PER-79c）")
        level = "yellow" if level == "green" else level
    if result["grey_zone"]["flag"]:
        flags.append(f"灰區 {len(result['grey_zone']['hits'])} 堂（說明段寫數字上限）")
        level = "yellow" if level == "green" else level
    pr = result.get("progress") or {}
    for b in pr.get("baselines") or []:
        v = (b.get("compare") or {}).get("verdict")
        if v in ("進步", "退步"):
            flags.append(f"基準測驗 {b['sport']} {v}（{b['date']}）")
            if v == "退步":
                level = "yellow" if level == "green" else level
    sw_down = [x for x in pr.get("swims") or [] if (x.get("compare") or {}).get("verdict") == "退步"]
    if len(sw_down) >= 2:
        flags.append(f"游泳同課型配速／划距退步 {len(sw_down)} 堂")
        level = "yellow" if level == "green" else level
    if (result.get("peaks") or {}).get("ftp_maybe_low"):
        flags.append(f"FTP 可能偏低：{((result.get('peaks') or {}).get('ftp_tier') or {}).get('tier')}（PROC-85g）")
        level = "yellow" if level == "green" else level
    row = {"athlete": requested, **ident, **result, "flags": flags, "level": level,
           "windows": {"recent": [window_start, end], "history": [hist_start, end]}}
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w-]+", "_", str(ident.get("athlete_name") or requested)).strip("_")
        p = save_dir / f"{slug}.json"
        p.write_text(json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
        row["saved_to"] = str(p)
    return row


async def _ef_gates(sport: str, ws: list[dict[str, Any]], window_start: str, a: dict[str, Any],
                    steady: list[dict[str, Any]], metrics: Any) -> dict[str, Any]:
    """PROC-84g: gate 1 = interval-session peak HR (window vs the earlier weeks), gate 2 =
    RPE of the EF-up session vs the earlier steady sessions of the same kind."""
    q = [w for w in ws if w.get("sport") == sport and ext.is_quality(w.get("title"))]
    recent = [w for w in q if str(w.get("date"))[:10] >= window_start][:3]
    base = [w for w in q if str(w.get("date"))[:10] < window_start][:6]
    r_hr = [v for v in [await session_max_hr(str(w.get("id"))) for w in recent] if v]
    b_hr = [v for v in [await session_max_hr(str(w.get("id"))) for w in base] if v]
    latest = a.get("latest") or {}
    rpe_l = await workout_rpe(latest.get("workout_id")) if latest.get("workout_id") else None
    earlier = [w for w in steady if str(w.get("date"))[:10] < str(latest.get("date"))][:4]
    rpe_b = [v for v in [await workout_rpe(str(w.get("id"))) for w in earlier] if v is not None]
    return ext.ef_gates(r_hr, b_hr, rpe_l, rpe_b)


async def _easy_runs(ws: list[dict[str, Any]], end: str, metrics: Any, keys: list[str],
                     power_runners: list[str]) -> dict[str, Any]:
    """PER-79b: flat easy runs, this week vs the 3 weeks before."""
    e = dt.date.fromisoformat(end)
    wk_from, base_from = (e - dt.timedelta(days=6)).isoformat(), (e - dt.timedelta(days=27)).isoformat()
    cands = [w for w in ws if w.get("sport") == "Run" and ext.is_easy_run(w.get("title"))
             and str(w.get("date"))[:10] >= base_from and (w.get("duration_actual") or 0) * 60 >= 20]
    rows = []
    for w in cands:
        r = dict(await metrics(w))
        title = str(w.get("title") or "")
        if any(k in title for k in ext.TREADMILL_WORDS):
            r["excluded"] = "跑步機"
        elif r.get("gain_m") and r.get("distance_km") and r["gain_m"] / r["distance_km"] > ext.FLAT_GAIN_M_PER_KM:
            r["excluded"] = f"非平路（爬升 {r['gain_m'] / r['distance_km']:.0f} m/km）"
        rows.append(r)
    wx = await easy_run_weather(keys, [r for r in rows if not r.get("excluded")])
    for r in rows:
        if r.get("workout_id") in wx:
            r["excluded"] = wx[r["workout_id"]]
    use_power = any(k and k in power_runners for k in keys)
    out = ext.easy_run_index([r for r in rows if r["date"] >= wk_from], [r for r in rows if r["date"] < wk_from],
                             use_power=use_power)
    if not wx and rows:
        out["weather_note"] = "天氣以 training-locations.json 座標＋跑步中點時間判；查不到座標或時間的課未排除"
    return out


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    """Keep the response small: drop per-session lists, keep verdicts."""
    out = {k: row.get(k) for k in ("athlete", "athlete_name", "athlete_id", "level", "flags", "saved_to")
           if row.get(k) is not None}
    for s in ("bike", "run"):
        a = row.get(s) or {}
        if a:
            lt = a.get("latest") or {}
            out[s] = {k: v for k, v in {
                "latest": {k2: lt.get(k2) for k2 in ("workout_id", "date", "title", "duration_min",
                                                     "decoupling_pct", "ef")} if lt else None,
                "decoupling_verdict": a.get("decoupling_verdict"),
                "decoupling_used": a.get("decoupling_used"),
                "decoupling_basis": a.get("decoupling_basis"),
                "segments": {k2: v2 for k2, v2 in {
                    "front_pct": ((lt.get("seg") or {}).get("front") or {}).get("decoupling_pct"),
                    "back": (lt.get("seg") or {}).get("back"),
                    "note": (lt.get("seg") or {}).get("note"),
                }.items() if v2 is not None} or None,
                "durability": a.get("durability"),
                "drift_into_heavy": a.get("drift_into_heavy"),
                "ef_gates": a.get("ef_gates"),
                "ef_change_pct": a.get("ef_change_pct"),
                "note": a.get("note") or a.get("ef_note"),
                "excluded": len(a.get("excluded") or []) or None,
            }.items() if v is not None}
    gz = row.get("grey_zone") or {}
    if gz:
        out["grey_zone"] = {"hits": gz.get("hits"), "flag": gz.get("flag")}
    if row.get("peaks"):
        out["peaks"] = row["peaks"]
    for k in ("easy_run", "wellness"):
        if row.get(k):
            out[k] = row[k]
    pr = row.get("progress") or {}
    if pr:
        out["progress"] = {k: v for k, v in {
            "logged": pr.get("logged"),
            "baselines": [{k2: b.get(k2) for k2 in ("sport", "date", "title", "metrics", "compare")}
                          for b in pr.get("baselines") or []] or None,
            "swims": [{k2: x.get(k2) for k2 in ("date", "title", "swim", "compare")}
                      for x in pr.get("swims") or []] or None,
            "weather": pr.get("weather") or None,
        }.items() if v}
    return out


def _table(rows: list[dict[str, Any]]) -> str:
    mark = {"red": "R", "yellow": "Y", "green": "G", "error": "!"}
    lines = ["燈|選手|騎 Pw:Hr|跑 Pa:Hr|EF Δ騎/跑|灰區|旗標"]
    for r in rows:
        def dec(s, r=r):
            a = r.get(s) or {}
            v = a.get("decoupling_used", (a.get("latest") or {}).get("decoupling_pct"))
            return f"{v:g}%" if v is not None else "-"

        def ef(s, r=r):
            v = (r.get(s) or {}).get("ef_change_pct")
            return f"{v:+g}%" if v is not None else "-"
        gz = r.get("grey_zone") or {}
        lines.append("|".join([mark.get(r.get("level"), "?"), str(r.get("athlete_name") or r.get("athlete")),
                               dec("bike"), dec("run"), f"{ef('bike')}/{ef('run')}",
                               str(len(gz.get("hits") or [])) if gz else "-",
                               "、".join(r.get("flags") or []) or "-"]))
    return "\n".join(lines)


async def lo_capability_scan(
    athletes: list[str],
    end: str | None = None,
    window_days: int = 14,
    history_days: int = 42,
    max_per_sport: int = 6,
    heat_c: float = 30.0,
    bike_if_max: float = 0.78,
    ef_up_pct: float = 3.0,
    peaks: str = "auto",
    save_dir: str | None = None,
    concurrency: int = 3,
    ledger: bool = True,
    swim: bool = True,
    weather: str = "auto",
    max_swims: int = 6,
    segments: bool = True,
    gates: bool = True,
    easy_index: bool = True,
    wellness: bool = True,
    power_runners: list[str] | None = None,
) -> dict[str, Any]:
    if not athletes:
        return {"isError": True, "error_code": "INVALID_ARGS", "message": "athletes is empty"}
    if weather not in ("auto", "never"):
        return {"isError": True, "error_code": "INVALID_ARGS", "message": "weather must be auto|never"}
    if peaks not in ("auto", "always", "never"):
        return {"isError": True, "error_code": "INVALID_ARGS", "message": "peaks must be auto|always|never"}
    end = end or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(end)
    except ValueError as e:
        return {"isError": True, "error_code": "INVALID_ARGS", "message": str(e)}
    sd = Path(save_dir).expanduser() if save_dir else None
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def run(a: str) -> dict[str, Any]:
        async with sem:
            return await _one(str(a).strip(), end=end, window_days=window_days, history_days=history_days,
                              max_per_sport=max_per_sport, heat_c=heat_c, bike_if_max=bike_if_max,
                              ef_up_pct=ef_up_pct, peaks=peaks, save_dir=sd, ledger=ledger, swim=swim,
                              weather=weather, max_swims=max_swims, segments=segments, gates=gates,
                              easy_index=easy_index, wellness=wellness, power_runners=power_runners)

    rows = await asyncio.gather(*(run(a) for a in athletes))
    order = {"error": 0, "red": 1, "yellow": 2, "green": 3}
    rows = sorted(rows, key=lambda r: order.get(r.get("level"), 9))
    return {
        "end": end, "count": len(rows),
        "levels": {k: sum(1 for r in rows if r.get("level") == k) for k in ("red", "yellow", "green", "error")},
        "table": _table(rows),
        "athletes": [_compact(r) for r in rows],
        "note": ("漂移／EF 取 TP 分析摘要的 Pw:Hr、Pa:Hr、EF（完整檔計算，非 12 秒取樣）。"
                 "穩態長課＝標題 有氧耐力／長距離騎乘／長跑（排除熱訓練、間歇類）。"
                 "TP 摘要沒有氣溫：熱天的紅燈是待確認，不是結論。閾值新鮮度（8f ③）仍走 athlete_card.py。"
                 "燈號只分流，裁決由教練做。"
                 "progress＝PROC-84d 帳本（learning/capability_log.jsonl）：本週新記的穩態課（含天氣）、"
                 "游泳（SWIM-91：配速／划頻／划距／SWOLF，同課型比較）、基準測驗（PROC-37b：同輸出比心率）。"
                 "6–8 週趨勢表＝專案 tools/progress_trend.py。"
                 "11/02：漂移改用逐秒資料（PROC-84h：剔前 10 分與停頓、>2 h 分前 2 h／後段，加長判準看前段，"
                 "後段＝耐久性；失水與補給工具看不到，人工排除）；EF 升先過兩關（PROC-84g）；FTP 三級（PROC-85g）；"
                 "輕鬆跑同配速心率（PER-79b）；靜止心率／HRV（PER-79c）。"),
    }


def register_lo_capability(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(Tool(
        name="lo_capability_scan",
        description=(
            "能力層 (sop-weekly 8f ② + 8g ④⑤) for a list of athletes in one call: the longest steady "
            "Z2 ride (≥90 min) / run (≥60 min) of the last window_days with TP's own Pw:Hr / Pa:Hr "
            "decoupling and the 5/8% verdict (≤5% twice in a row = may extend), EF vs the earlier "
            "same-kind sessions (>+3% = threshold may be low), grey-zone sweep of easy sessions "
            "(bike IF > 0.78, run avg speed above the Z2 ceiling; ≥2 hits = flag), and power-20min "
            "peaks vs FTP in three tiers (<3% noise / 3–8% needs a 2nd source / ≥8%, PROC-85g) when EF jumps "
            "and passes the PROC-84g peak-HR/RPE gates (peaks=auto) or always. 11/02: decoupling from the 1 s "
            "series (PROC-84h), easy-run HR at the same pace (PER-79b), resting HR / HRV (PER-79c). "
            "Reads TP's analysis summary "
            "(full-file numbers, no 12 s samples). Read-only. One compact row per athlete + text "
            "table; full JSON per athlete in save_dir. PROC-84d/98, SWIM-91: also logs every newly analysed "
            "steady session (with weather), swim (pace/stroke rate/DPS/SWOLF) and 基準測驗 baseline (HR at the "
            "same output) to learning/capability_log.jsonl and compares with earlier entries."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "athletes": {"type": "array", "items": {"type": "string"}},
                "end": {"type": "string", "description": "YYYY-MM-DD, default today."},
                "window_days": {"type": "integer", "default": 14},
                "history_days": {"type": "integer", "default": 42,
                                 "description": "EF baseline window (the window plus ~4 weeks)."},
                "max_per_sport": {"type": "integer", "default": 6},
                "heat_c": {"type": "number", "default": 30},
                "bike_if_max": {"type": "number", "default": 0.78},
                "ef_up_pct": {"type": "number", "default": 3},
                "peaks": {"type": "string", "enum": ["auto", "always", "never"], "default": "auto"},
                "save_dir": {"type": "string", "description": "Directory ON THE MACHINE RUNNING THIS SERVER."},
                "concurrency": {"type": "integer", "default": 3},
                "ledger": {"type": "boolean", "default": True,
                           "description": "PROC-84d: append analysed sessions to learning/capability_log.jsonl."},
                "swim": {"type": "boolean", "default": True,
                         "description": "SWIM-91: analyse swims in the window (pace, stroke rate, DPS, SWOLF)."},
                "weather": {"type": "string", "enum": ["auto", "never"], "default": "auto",
                            "description": "PROC-93 weather for steady sessions in the window; "
                                           "hot/humid ones leave the trend."},
                "max_swims": {"type": "integer", "default": 6},
                "segments": {"type": "boolean", "default": True,
                             "description": "PROC-84h: decoupling from the 1 s series (first 10 min out, stops out, "
                                            ">2 h split into first 2 h / after; durability + drift flags)."},
                "gates": {"type": "boolean", "default": True,
                          "description": "PROC-84g: EF rise must pass interval peak-HR and RPE gates."},
                "easy_index": {"type": "boolean", "default": True,
                               "description": "PER-79b: flat easy-run HR÷speed this week vs 3-week mean ± SD."},
                "wellness": {"type": "boolean", "default": True,
                             "description": "PER-79c: resting HR 7-day +3 bpm, HRV saturation pattern."},
                "power_runners": {"type": "array", "items": {"type": "string"},
                                  "description": "Athletes whose PER-79b index uses NP instead of speed."},
            },
            "required": ["athletes"],
        },
    ))

    async def _h(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_capability_scan(
            athletes=list(args.get("athletes") or []), end=args.get("end"),
            window_days=int(args.get("window_days", 14)), history_days=int(args.get("history_days", 42)),
            max_per_sport=int(args.get("max_per_sport", 6)), heat_c=float(args.get("heat_c", 30)),
            bike_if_max=float(args.get("bike_if_max", 0.78)), ef_up_pct=float(args.get("ef_up_pct", 3)),
            peaks=str(args.get("peaks", "auto")), save_dir=args.get("save_dir"),
            concurrency=int(args.get("concurrency", 3)),
            ledger=bool(args.get("ledger", True)), swim=bool(args.get("swim", True)),
            weather=str(args.get("weather", "auto")), max_swims=int(args.get("max_swims", 6)),
            segments=bool(args.get("segments", True)), gates=bool(args.get("gates", True)),
            easy_index=bool(args.get("easy_index", True)), wellness=bool(args.get("wellness", True)),
            power_runners=list(args.get("power_runners") or []) or None,
        )

    handlers["lo_capability_scan"] = _h
