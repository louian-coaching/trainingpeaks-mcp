"""羅教練 fork-only: ``lo_race_metrics`` — PROC-41g 鐵人賽複盤的兩項指標（2026/11/02 教練選單裁示）.

**Context.** Same average bike power ridden in surges (40–140% MAP) left the following
9.3 km run 42 s slower than even pacing, 35 s of it in the first half (Etxebarria 2013 /
2014). So when a triathlon run starts ≥3% slower than the race plan, the review has to
look at the bike's surges before calling it a running problem. And for 226 runs (and
marathons) the review records Smyth 2022's decoupling ratio — (HR ÷ speed at 35–40 km) ÷
(HR ÷ speed at 5–10 km), <1.10 low / 1.10–1.19 mid / ≥1.20 high, plus the first km where
it climbs past 1.025 and stays there (82,303 marathoners; onset 33 km in the low group,
19 km in the high one). Shorter runs use the same windows scaled to the run's length and
are only compared with the athlete's own runs of that distance.

The >120% FTP surge line is an assistant operating threshold, not a literature value.
Weather (PROC-93) comes attached to the run so a hot day is read as heat first.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from tp_mcp.tools import lo_capability_ext as ext

logger = logging.getLogger(__name__)

SURGE_PCT = 120.0
SURGE_SMOOTH_S = 3.0
SURGE_MIN_S = 3.0
SURGE_MERGE_GAP_S = 10.0
FULL_KM = 40.0
SMYTH_REF, SMYTH_END = (5.0, 10.0), (35.0, 40.0)
MARATHON_KM = 42.195
ONSET_RATIO = 1.025
ONSET_MIN_KM = 3
BANDS = (1.10, 1.20)


def _err(code: str, message: str) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message}


def bike_surges(points: list[dict[str, Any]], ftp: float, pct: float = SURGE_PCT) -> dict[str, Any]:
    """Time and number of efforts with 3-s power above ``pct``% FTP; first/second-half power."""
    pts, _ = ext.prep_points(points)
    pts = [p for p in pts if isinstance(p.get("Power"), (int, float))]
    if len(pts) < 30 or not ftp:
        return {"note": "沒有逐秒功率或缺 FTP"}
    line = ftp * pct / 100.0
    win: deque[tuple[float, float]] = deque()
    s = 0.0
    above_s, episodes, run_s, gap_s, in_ep = 0.0, 0, 0.0, 0.0, False
    for i, p in enumerate(pts):
        t, w = float(p["time"]), float(p["Power"])
        win.append((t, w))
        s += w
        while win and t - win[0][0] >= SURGE_SMOOTH_S:
            s -= win.popleft()[1]
        roll = s / len(win)
        dt_s = t - float(pts[i - 1]["time"]) if i else 0.0
        if dt_s <= 0 or dt_s > ext.GAP_S:
            dt_s = 0.0
        if roll >= line:
            above_s += dt_s
            run_s += dt_s
            gap_s = 0.0
            if not in_ep and run_s >= SURGE_MIN_S:
                in_ep = True
                episodes += 1
        else:
            gap_s += dt_s
            if gap_s >= SURGE_MERGE_GAP_S:
                in_ep, run_s = False, 0.0
    t0, t1 = pts[0]["time"], pts[-1]["time"]
    mid = t0 + (t1 - t0) / 2

    def half_hr(lo: float, hi: float) -> float | None:
        v = [float(p["HeartRate"]) for p in pts if lo <= p["time"] <= hi
             and isinstance(p.get("HeartRate"), (int, float)) and p["HeartRate"] > 0]
        return round(sum(v) / len(v), 1) if v else None

    def half_w(lo: float, hi: float) -> float | None:
        acc = [(p["time"], p["AccumulatedPower"]) for p in pts if lo <= p["time"] <= hi
               and isinstance(p.get("AccumulatedPower"), (int, float))]
        if len(acc) >= 2 and acc[-1][0] > acc[0][0] and acc[-1][1] >= acc[0][1]:
            return round((acc[-1][1] - acc[0][1]) / (acc[-1][0] - acc[0][0]), 1)
        vals = [float(p["Power"]) for p in pts if lo <= p["time"] <= hi]
        return round(sum(vals) / len(vals), 1) if vals else None

    h1, h2 = half_w(t0, mid), half_w(mid, t1)
    out: dict[str, Any] = {
        "ftp": ftp, "surge_line_w": round(line), "surge_pct": pct,
        "minutes_above": round(above_s / 60, 1), "episodes": episodes,
        "first_half_w": h1, "second_half_w": h2,
        "half_change_pct": round(100 * (h2 / h1 - 1), 1) if h1 and h2 else None,
    }
    out["first_half_hr"], out["second_half_hr"] = half_hr(t0, mid), half_hr(mid, t1)
    return out


def _time_at_km(pts: list[dict[str, Any]], scale: float, d0: float, km: float) -> float | None:
    target = d0 + km * 1000.0 / scale
    for p in pts:
        d = p.get("Distance")
        if isinstance(d, (int, float)) and d >= target:
            return float(p["time"])
    return None


def run_decoupling(points: list[dict[str, Any]]) -> dict[str, Any]:
    pts, scale = ext.prep_points(points)
    dist = [p for p in pts if isinstance(p.get("Distance"), (int, float))]
    if len(dist) < 30:
        return {"note": "沒有逐秒距離資料"}
    flags = ext.moving_flags(pts, scale)
    d0 = dist[0]["Distance"]                          # multisport child files carry the race odometer
    total_km = (dist[-1]["Distance"] - d0) * scale / 1000.0
    if total_km < 5:
        return {"note": f"跑段 {total_km:.1f} km，太短不算脫鉤比"}
    if total_km >= FULL_KM:
        ref, end, scaled = SMYTH_REF, SMYTH_END, False
    else:
        f = total_km / MARATHON_KM
        ref = (SMYTH_REF[0] * f, SMYTH_REF[1] * f)
        end = (SMYTH_END[0] * f, SMYTH_END[1] * f)
        scaled = True

    def win(k0: float, k1: float) -> dict[str, Any] | None:
        ta, tb = _time_at_km(pts, scale, d0, k0), _time_at_km(pts, scale, d0, k1)
        if ta is None or tb is None or tb <= ta:
            return None
        return ext.segment_stats(pts, flags, scale, "Run", ta, tb, min_moving_s=60)   # 1 km ≈ 4–7 min

    r, e = win(*ref), win(*end)
    out: dict[str, Any] = {"total_km": round(total_km, 2),
                           "ref_km": [round(ref[0], 1), round(ref[1], 1)],
                           "end_km": [round(end[0], 1), round(end[1], 1)],
                           "scaled_to_distance": scaled}
    if not (r and e):
        out["note"] = "參考段或末段資料不足"
        return out
    ref_i = r["avg_hr"] / r["avg_speed_ms"]
    ratio = (e["avg_hr"] / e["avg_speed_ms"]) / ref_i
    out["ratio"] = round(ratio, 3)
    out["band"] = "低（<1.10）" if ratio < BANDS[0] else ("中（1.10–1.19）" if ratio < BANDS[1] else "高（≥1.20）")
    per_km = []
    k = int(ref[1])
    while k + 1 <= total_km:
        w = win(k, k + 1)
        if w:
            per_km.append((k + 1, (w["avg_hr"] / w["avg_speed_ms"]) / ref_i))
        k += 1
    roll = [(km, sum(x for _, x in per_km[max(0, i - 2):i + 1]) / len(per_km[max(0, i - 2):i + 1]))
            for i, (km, _) in enumerate(per_km)]
    # "first passes 1.025 and stays there" — a last-km spike is not an onset: ≥3 km must follow
    onset = next((km for i, (km, v) in enumerate(roll) if v > ONSET_RATIO and len(roll) - i >= ONSET_MIN_KM
                  and all(v2 > ONSET_RATIO for _, v2 in roll[i:])), None)
    out["onset_km"] = onset
    out["per_km_ratio"] = [[km, round(v, 3)] for km, v in per_km]
    half = total_km / 2
    h1, h2 = win(0.0, half), win(half, total_km - 0.01)
    if h1 and h2:
        out["first_half"] = {"pace_s_km": round(1000 / h1["avg_speed_ms"]), "avg_hr": h1["avg_hr"]}
        out["second_half"] = {"pace_s_km": round(1000 / h2["avg_speed_ms"]), "avg_hr": h2["avg_hr"]}
        out["second_half_slower_pct"] = round(100 * (h1["avg_speed_ms"] / h2["avg_speed_ms"] - 1), 1)
    return out


async def lo_race_metrics(bike_workout_id: str | None = None, run_workout_id: str | None = None,
                          ftp: float | None = None, surge_pct: float = SURGE_PCT) -> dict[str, Any]:
    from tp_mcp.tools import lo_progress as lp
    from tp_mcp.tools.analyze import tp_analyze_workout
    from tp_mcp.tools.lo_weather import weather_brief

    if not bike_workout_id and not run_workout_id:
        return _err("INVALID_ARGS", "give bike_workout_id and/or run_workout_id")
    out: dict[str, Any] = {"rule": "PROC-41g"}
    if bike_workout_id:
        if not ftp:
            from tp_mcp.tools.settings import tp_get_athlete_settings

            st = await tp_get_athlete_settings()
            for g in ((st or {}).get("settings") or {}).get("powerZones") or []:
                if isinstance(g, dict) and g.get("workoutTypeId") in (2, 0) and g.get("threshold"):
                    ftp = float(g["threshold"])
                    break
        an = await tp_analyze_workout(str(bike_workout_id))
        if an.get("isError"):
            out["bike"] = {"error": an.get("message")}
        else:
            out["bike"] = {"workout_id": str(bike_workout_id), **bike_surges(lp._points(an), ftp or 0.0, surge_pct)}
    if run_workout_id:
        an = await tp_analyze_workout(str(run_workout_id))
        if an.get("isError"):
            out["run"] = {"error": an.get("message")}
        else:
            out["run"] = {"workout_id": str(run_workout_id), **run_decoupling(lp._points(an))}
            try:
                out["run"]["weather"] = await weather_brief(str(run_workout_id), an)
            except Exception:  # noqa: BLE001
                logger.exception("race weather failed")
    out["how_to_read"] = (
        "跑段前半比計畫慢 ≥3%：先看 bike.minutes_above／episodes 和本人同距離過往比賽比，明顯偏多＝"
        "原因寫「騎段功率起伏」，不直接寫跑力不足（120% 是助理操作門檻）。run.ratio：<1.10 低｜1.10–1.19 中｜"
        "≥1.20 高；onset_km 早於 20 km 且偏高＝下場前半目標與補給列檢討；scaled_to_distance=true 的只和本人"
        "同距離比。熱天照 PROC-93 先歸環境。"
    )
    return out


def register_lo_race(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(Tool(
        name="lo_race_metrics",
        description=(
            "PROC-41g triathlon race-review numbers from the 1 s files: bike surges (minutes and efforts "
            "with 3-s power above surge_pct% FTP, first/second-half power) and run decoupling (Smyth 2022 "
            "ratio = HR÷speed at 35–40 km over 5–10 km, scaled to shorter runs; band <1.10/1.10–1.19/≥1.20; "
            "onset km past 1.025; first/second-half pace) with the run's weather. FTP defaults to the "
            "athlete's bike FTP. Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bike_workout_id": {"type": "string"},
                "run_workout_id": {"type": "string"},
                "ftp": {"type": "number", "description": "Bike FTP; default = athlete settings."},
                "surge_pct": {"type": "number", "default": SURGE_PCT},
            },
        },
    ))

    async def _h(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_race_metrics(
            bike_workout_id=args.get("bike_workout_id"), run_workout_id=args.get("run_workout_id"),
            ftp=float(args["ftp"]) if args.get("ftp") else None,
            surge_pct=float(args.get("surge_pct", SURGE_PCT)),
        )

    handlers["lo_race_metrics"] = _h
