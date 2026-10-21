"""羅教練 fork-only: progress ledger helpers for ``lo_capability_scan`` (PROC-84d／PROC-37b／SWIM-91).

**Context (2026/10/21 教練裁示).** The weekly 能力層 (PROC-84) only ever looked at a 2–6
week window and wrote its verdicts as prose into progress files, so "has this athlete
improved over the last three months, and where is he stuck" had no answer. Swim had no
progress signal at all (no heart rate in the water, so 8f skipped it).

This module adds:

* ``capability_log.jsonl`` (learning dir, next to heat_log): one line per analysed session —
  steady Z2 rides/runs (EF, decoupling, weather verdict), swims (pace, stroke rate, strokes
  per 100 m, distance per stroke, SWOLF per 25 m) and 基準測驗 baseline sessions (HR at the
  fixed power / pace of the main segment). Idempotent per (athlete, workout).
* ``swim_metrics`` — from the analysis laps, no heart rate (SWIM rule: swim ignores HR).
* ``baseline_metrics`` — main-segment averages from the analysis time series, warm-up and
  cool-down trimmed per the PROC-37b protocol.
* ``compare_baseline`` / ``compare_swim`` — the PER-79 trend verdicts (進步／持平／退步).

Thresholds here mirror method/5 §5.13 — change both together.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from tp_mcp.tools.lo_weather import learning_dir

LEDGER = "capability_log.jsonl"
BASELINE_TITLE = "基準測驗"
# PROC-37b protocol trims (main segment only)
BIKE_WARM_S, BIKE_COOL_S = 15 * 60, 5 * 60
RUN_WARM_M, RUN_COOL_M = 2000.0, 1000.0
# PER-79 trend thresholds
HR_DELTA_BPM = 3.0          # baseline: same power/pace, HR ±3 bpm
TARGET_TOL = 0.03           # baseline main-segment output must be within ±3 % of the previous one
EF_DELTA_PCT = 3.0
SWIM_PACE_DELTA_S = 2.0     # s/100 m at the same session type
SWIM_DPS_DELTA_PCT = 3.0


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------
def ledger_path() -> Path | None:
    d = learning_dir()
    return None if d is None else d / LEDGER


def read_ledger(athlete_id: str | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    p = path or ledger_path()
    if p is None or not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if athlete_id is None or str(r.get("athlete_id")) == str(athlete_id):
            rows.append(r)
    return rows


def logged_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {str(r.get("workout_id")) for r in rows}


def append_ledger(records: list[dict[str, Any]], path: Path | None = None) -> str | None:
    p = path or ledger_path()
    if p is None or not records:
        return None if p is None else str(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    seen = {(str(r.get("athlete_id")), str(r.get("workout_id"))) for r in read_ledger(path=p)}
    with p.open("a", encoding="utf-8") as fh:
        for r in records:
            k = (str(r.get("athlete_id")), str(r.get("workout_id")))
            if k in seen:
                continue
            seen.add(k)
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(p)


# ---------------------------------------------------------------------------
# Swim (no heart rate)
# ---------------------------------------------------------------------------
def swim_metrics(analysis: dict[str, Any]) -> dict[str, Any] | None:
    """Work laps = ≥100 m, moving, with a stroke rate. Compact laps carry meters in ``km``
    for swims, ``pace_s`` per 100 m and ``cad`` = stroke rate (device definition: watch-arm
    cycles / min). Kick / drill laps (no stroke rate, or >1.35× the median pace) drop out."""
    laps = [lp for lp in (analysis.get("laps") or []) if isinstance(lp, dict)]
    work = [lp for lp in laps
            if (lp.get("km") or 0) >= 100 and (lp.get("pace_s") or 0) > 0 and (lp.get("cad") or 0) > 0]
    if not work:
        return None
    med = statistics.median(lp["pace_s"] for lp in work)
    work = [lp for lp in work if lp["pace_s"] <= med * 1.35]
    dist = sum(lp["km"] for lp in work)
    pace = sum(lp["pace_s"] * lp["km"] for lp in work) / dist                 # distance-weighted s/100 m
    rate = sum(lp["cad"] * lp["km"] for lp in work) / dist                    # cycles / min
    strokes_100 = rate * pace / 60.0
    return {
        "work_m": int(dist), "work_laps": len(work),
        "pace_s_100": round(pace, 1), "stroke_rate": round(rate, 1),
        "strokes_per_100": round(strokes_100, 1),
        "dps_m": round(100.0 / strokes_100, 2) if strokes_100 else None,
        "swolf25": round(pace / 4 + strokes_100 / 4, 1),
        "pace_drop_pct": round(100 * (work[-1]["pace_s"] / work[0]["pace_s"] - 1), 1) if len(work) >= 2 else None,
    }


def compare_swim(new: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Same title type, earlier swims: pace faster by ≥2 s/100 m or DPS +3 % at a pace no
    slower = 進步; the reverse = 退步."""
    prev = [h["swim"] for h in history if h.get("swim") and h["swim"].get("pace_s_100")]
    if len(prev) < 2 or not new.get("pace_s_100"):
        return None
    p0 = statistics.median(x["pace_s_100"] for x in prev)
    d0 = statistics.median(x["dps_m"] for x in prev if x.get("dps_m")) if any(x.get("dps_m") for x in prev) else None
    dp = new["pace_s_100"] - p0
    ddps = 100 * (new["dps_m"] / d0 - 1) if d0 and new.get("dps_m") else None
    if dp <= -SWIM_PACE_DELTA_S or (ddps is not None and ddps >= SWIM_DPS_DELTA_PCT and dp <= 0):
        v = "進步"
    elif dp >= SWIM_PACE_DELTA_S or (ddps is not None and ddps <= -SWIM_DPS_DELTA_PCT and dp >= 0):
        v = "退步"
    else:
        v = "持平"
    return {"n_prev": len(prev), "pace_delta_s": round(dp, 1),
            "dps_delta_pct": None if ddps is None else round(ddps, 1), "verdict": v}


# ---------------------------------------------------------------------------
# Baseline sessions (PROC-37b)
# ---------------------------------------------------------------------------
def _points(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    f = analysis.get("data_file")
    if not f:
        return []
    try:
        raw = json.loads(Path(f).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pts = [p for p in (raw.get("data") or []) if isinstance(p, dict) and isinstance(p.get("time"), (int, float))]
    return sorted(pts, key=lambda p: p["time"])


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def baseline_metrics(sport: str, analysis: dict[str, Any],
                     points: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Main-segment averages. Bike: drop first 15 min / last 5 min. Run: drop first 2 km /
    last 1 km. Swim: lap metrics (4×200 protocol)."""
    if sport == "Swim":
        laps = [lp for lp in (analysis.get("laps") or []) if isinstance(lp, dict)]
        main = [lp for lp in laps if lp.get("km") == 200]           # PROC-37b main set = 4×200
        m = swim_metrics({"laps": main} if len(main) >= 3 else analysis)
        return {"swim": m} if m else None
    pts = points if points is not None else _points(analysis)
    if not pts:
        return None
    t0, t1 = pts[0]["time"], pts[-1]["time"]
    if sport == "Bike":
        main = [p for p in pts if t0 + BIKE_WARM_S <= p["time"] <= t1 - BIKE_COOL_S]
    else:
        dist = [p for p in pts if isinstance(p.get("Distance"), (int, float))]
        if not dist:
            return None
        d0, d1 = dist[0]["Distance"], dist[-1]["Distance"]
        main = [p for p in dist if d0 + RUN_WARM_M <= p["Distance"] <= d1 - RUN_COOL_M]
    main = [p for p in main if isinstance(p.get("HeartRate"), (int, float)) and p["HeartRate"] > 0]
    if len(main) < 10:
        return None
    out_key = "Power" if sport == "Bike" else "Speed"
    vals = [p for p in main if isinstance(p.get(out_key), (int, float)) and p[out_key] > 0]
    if len(vals) < 10:
        return None
    half = len(vals) // 2
    def ef(seg: list[dict[str, Any]]) -> float:
        return _mean([p[out_key] for p in seg]) / _mean([p["HeartRate"] for p in seg])
    ef1, ef2 = ef(vals[:half]), ef(vals[half:])
    out = {
        "main_min": round((main[-1]["time"] - main[0]["time"]) / 60, 1),
        "avg_hr": round(_mean([p["HeartRate"] for p in vals]), 1),
        "decoupling_pct": round(100 * (ef1 / ef2 - 1), 1) if ef2 else None,
    }
    if sport == "Bike":
        out["avg_w"] = round(_mean([p["Power"] for p in vals]), 1)
        out["ef"] = round(out["avg_w"] / out["avg_hr"], 3)
    else:
        sp = _mean([p["Speed"] for p in vals])
        out["avg_speed_ms"] = round(sp, 3)
        out["pace_s_km"] = round(1000 / sp, 1) if sp else None
        out["ef"] = round(sp * 60 / out["avg_hr"], 3)        # m/min per bpm
    return out


def compare_baseline(sport: str, new: dict[str, Any], prev: dict[str, Any] | None,
                     wx_new: dict[str, Any] | None = None, wx_prev: dict[str, Any] | None = None) -> dict[str, Any]:
    """PER-79: same output (±3 %) → HR −3 bpm = 進步, +3 = 退步. Hot / humid outdoor
    sessions (PROC-93 correct_interpretation) are not compared."""
    if prev is None:
        return {"verdict": "首次（建立基準）"}
    if sport == "Swim":
        a, b = (new.get("swim") or {}), (prev.get("swim") or {})
        c = compare_swim(a, [{"swim": b}, {"swim": b}]) if a and b else None   # one prior = the reference
        if c:
            c.pop("n_prev", None)
        return {"verdict": c["verdict"] if c else "資料不足", **(c or {})}
    if (wx_new or {}).get("correct_interpretation") or (wx_prev or {}).get("correct_interpretation"):
        return {"verdict": "熱／濕熱，不比（PROC-93）"}
    k = "avg_w" if sport == "Bike" else "avg_speed_ms"
    if not new.get(k) or not prev.get(k):
        return {"verdict": "資料不足"}
    drift = new[k] / prev[k] - 1
    if abs(drift) > TARGET_TOL:
        return {"verdict": f"輸出差 {drift * 100:+.1f}%（>±3%），不比心率", "output_delta_pct": round(drift * 100, 1)}
    dhr = new["avg_hr"] - prev["avg_hr"]
    v = "進步" if dhr <= -HR_DELTA_BPM else ("退步" if dhr >= HR_DELTA_BPM else "持平")
    return {"verdict": v, "hr_delta_bpm": round(dhr, 1), "output_delta_pct": round(drift * 100, 1),
            "decoupling_delta": (None if new.get("decoupling_pct") is None or prev.get("decoupling_pct") is None
                                 else round(new["decoupling_pct"] - prev["decoupling_pct"], 1))}
