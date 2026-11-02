"""羅教練 fork-only: 2026/11/02 additions to the weekly 能力層 (``lo_capability_scan``).

**Context (2026/11/02 教練選單裁示, after the cycling / triathlon analysis literature
review).** Five judgement rules were adopted that the scan could not apply because it
only read TP's whole-session summary:

* **PROC-84h** ``segment_decoupling`` — decoupling from the 1 s time series on the steady
  part only: the first 10 min are dropped, stops (<3 km/h) are removed, and a session
  longer than 2 h is split into 前 2 小時 (the 加長判準 ≤5% / 5–8% / >8% applies to this
  part) and 2 小時後 (耐久性解耦 = how far EF fell in the back against the front; two in a
  row >8% = 耐久性弱項). A back part whose HR sits above fresh VT1 (LTHR × 0.89 when no VT1
  test exists — an assistant approximation, not literature) while the output still hugs
  the Z2 ceiling is flagged 漂入中強度. Prolonged cycling moves VT1 power down ~10% in 2 h
  and HR decoupling tracks that loss (r = −0.76, Rothschild 2025).
* **PROC-85g** ``ftp_tier`` — FTP "may be low" is reported in three tiers instead of one
  ×1.03 line: <3% = noise (home 20-min TT CV 3.7%, Matta 2022), 3–8% = needs a second
  source in the same direction, ≥8% = one source is enough. Peaks from sessions averaging
  <5 °C or >30 °C are skipped.
* **PROC-84g** ``ef_gates`` — an EF rise >3% only counts as "threshold may be low" when
  (1) interval-session peak HR has not dropped ≥5 bpm against the previous 4 weeks and
  (2) RPE at the same output has not risen ≥1. Fatigue lowers HR and fakes EF gains
  (Hammes 2016, Sanders 2018, Coates 2018).
* **PER-79b** ``easy_run_index`` — HR ÷ speed of flat easy runs this week against the
  previous 3 weeks (mean ± SD): >1 SD = abnormal. Hot (WBGT ≥23), cold (≤5 °C) and
  treadmill runs are left out; <2 runs this week or <4 in the base = no verdict.
* **PER-79c** ``wellness_signal`` — resting HR 7-day mean ≥ +3 bpm over the 4-week base
  = 黃; HRV weekly mean above the 4-week mean + 0.5 SD while resting HR falls = 副交感飽和
  candidate (red once the athlete also reports more fatigue — the tool cannot see that);
  <3 valid HRV days in the week = no HRV verdict.

Thresholds mirror method/5 §5.13-4 and §5.13-6 — change both together.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

# PROC-84h
SKIP_S = 600                 # first 10 min out
SPLIT_S = 7200               # 前 2 小時 | 2 小時後
MIN_BACK_S = 1200            # a back part shorter than 20 min is not a back part
MIN_SEG_MOVING_S = 600       # a half needs ≥10 min of moving time
STOP_MS = 3.0 / 3.6          # <3 km/h = stopped
GAP_S = 30.0                 # sample gaps longer than this are pauses, not riding
LOOKBACK_S = 10.0
VT1_FROM_LTHR = 0.89         # assistant approximation when no VT1 test exists
DRIFT_Z2_FRAC = 0.97         # "still hugging the Z2 ceiling"
DURABILITY_WEAK_PCT = 8.0
# PROC-85g
FTP_TIERS = (3.0, 8.0)
PEAK_TEMP_LO_C, PEAK_TEMP_HI_C = 5.0, 30.0
# PROC-84g
PEAK_HR_DROP_BPM = 5.0
RPE_RISE = 1.0
QUALITY_WORDS = ("間歇", "閾值", "VO2", "比賽強度", "速度")
# PER-79b
EASY_SD = 1.0
EASY_MIN_WEEK, EASY_MIN_BASE = 2, 4
FLAT_GAIN_M_PER_KM = 10.0
EASY_TITLES = ("輕鬆跑", "有氧耐力")
TREADMILL_WORDS = ("跑步機", "treadmill", "Treadmill")
# PER-79c
RHR_RISE_BPM = 3.0
HRV_SD_FRAC = 0.5
HRV_MIN_DAYS = 3


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# PROC-84h — segment decoupling
# ---------------------------------------------------------------------------
def prep_points(points: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
    """Sorted timed samples + the factor that turns ``Distance`` into metres
    (TP charts report km; test fixtures sometimes metres)."""
    pts = sorted((p for p in points or [] if isinstance(p, dict) and isinstance(p.get("time"), (int, float))),
                 key=lambda p: p["time"])
    ds = [p["Distance"] for p in pts if isinstance(p.get("Distance"), (int, float))]
    scale = 1000.0 if ds and (max(ds) - min(ds)) < 300 else 1.0
    return pts, scale


def moving_flags(pts: list[dict[str, Any]], scale: float) -> list[bool]:
    """True = moving. Speed over a ≥10 s look-back from the Distance channel (unit-safe);
    a file without Distance (indoor, no speed sensor) counts as moving throughout."""
    if not any(isinstance(p.get("Distance"), (int, float)) for p in pts):
        return [True] * len(pts)
    flags, j = [], 0
    for i, p in enumerate(pts):
        d = p.get("Distance")
        if not isinstance(d, (int, float)):
            flags.append(True)
            continue
        while j < i - 1 and p["time"] - pts[j + 1]["time"] >= LOOKBACK_S:
            j += 1
        q = pts[j]
        dt_s = p["time"] - q["time"]
        dq = q.get("Distance")
        if dt_s <= 0 or not isinstance(dq, (int, float)):
            flags.append(True)
            continue
        flags.append((d - dq) * scale / dt_s >= STOP_MS)
    return flags


def segment_stats(pts: list[dict[str, Any]], flags: list[bool], scale: float, sport: str,
                  t_a: float, t_b: float, min_moving_s: float = MIN_SEG_MOVING_S) -> dict[str, Any] | None:
    """Moving-time averages between ``t_a`` and ``t_b``. Bike power from
    ``AccumulatedPower`` deltas when present (TP TotalWork basis, coasting zeros
    included), else the Power samples; run speed from Distance deltas."""
    mov = hr_w = hr_t = work = work_t = dist = dist_t = 0.0
    for i in range(1, len(pts)):
        p, q = pts[i], pts[i - 1]
        if not (t_a < p["time"] <= t_b):
            continue
        dt_s = p["time"] - q["time"]
        if dt_s <= 0 or dt_s > GAP_S or not flags[i]:
            continue
        mov += dt_s
        hr = p.get("HeartRate")
        if isinstance(hr, (int, float)) and hr > 0:
            hr_w += hr * dt_s
            hr_t += dt_s
        if sport == "Bike":
            a1, a0 = p.get("AccumulatedPower"), q.get("AccumulatedPower")
            if isinstance(a1, (int, float)) and isinstance(a0, (int, float)) and a1 >= a0:
                work += a1 - a0
                work_t += dt_s
            elif isinstance(p.get("Power"), (int, float)) and p["Power"] >= 0:
                work += p["Power"] * dt_s
                work_t += dt_s
        else:
            d1, d0 = p.get("Distance"), q.get("Distance")
            if isinstance(d1, (int, float)) and isinstance(d0, (int, float)) and d1 >= d0:
                dist += (d1 - d0) * scale
                dist_t += dt_s
    if mov < min_moving_s or hr_t < 0.8 * mov:
        return None
    avg_hr = hr_w / hr_t
    out: dict[str, Any] = {"moving_min": round(mov / 60, 1), "avg_hr": round(avg_hr, 1)}
    if sport == "Bike":
        if work_t < 0.8 * mov or work <= 0:
            return None
        w = work / work_t
        out["avg_w"] = round(w, 1)
        out["ef"] = round(w / avg_hr, 4)
    else:
        if dist_t < 0.8 * mov or dist <= 0:
            return None
        sp = dist / dist_t
        out["avg_speed_ms"] = round(sp, 3)
        out["ef"] = round(sp * 60 / avg_hr, 4)        # m/min per bpm
    return out


def segment_decoupling(points: list[dict[str, Any]], sport: str, *, vt1_hr: float | None = None,
                       z2_upper: float | None = None) -> dict[str, Any]:
    """PROC-84h. ``z2_upper``: W (bike) or m/s (run). Decoupling % = first-half EF ÷
    second-half EF − 1 (same formula as the 基準測驗 ledger)."""
    pts, scale = prep_points(points)
    if len(pts) < 10:
        return {"rule": "PROC-84h", "note": "沒有逐秒資料，沿用 TP 摘要 Pw:Hr／Pa:Hr"}
    flags = moving_flags(pts, scale)
    t0, t1 = pts[0]["time"], pts[-1]["time"]
    start = t0 + SKIP_S
    if t1 - start < 2 * MIN_SEG_MOVING_S:
        return {"rule": "PROC-84h", "note": "扣掉前 10 分後不足 20 分，不分段"}
    long = (t1 - t0) >= SPLIT_S + MIN_BACK_S
    f_end = t0 + SPLIT_S if long else t1
    mid = start + (f_end - start) / 2
    out: dict[str, Any] = {"rule": "PROC-84h", "skip_min": SKIP_S // 60, "long": long}
    a = segment_stats(pts, flags, scale, sport, start, mid)
    b = segment_stats(pts, flags, scale, sport, mid, f_end)
    key = "avg_w" if sport == "Bike" else "avg_speed_ms"
    if a and b:
        out["front"] = {"decoupling_pct": round(100 * (a["ef"] / b["ef"] - 1), 1),
                        "minutes": round(a["moving_min"] + b["moving_min"], 1),
                        "halves": [{k: s[k] for k in ("avg_hr", key)} for s in (a, b)]}
    else:
        out["front_note"] = "前段穩定資料不足（心率或功率／距離缺漏）"
    if long:
        fr = segment_stats(pts, flags, scale, sport, start, f_end)
        bk = segment_stats(pts, flags, scale, sport, f_end, t1)
        if fr and bk:
            back: dict[str, Any] = {"minutes": bk["moving_min"], "avg_hr": bk["avg_hr"], key: bk[key],
                                    "durability_pct": round(100 * (fr["ef"] / bk["ef"] - 1), 1)}
            if vt1_hr and z2_upper:
                back["vt1_hr"] = round(vt1_hr)
                back["drift_into_heavy"] = bool(bk["avg_hr"] > vt1_hr and bk[key] >= DRIFT_Z2_FRAC * z2_upper)
            out["back"] = back
        else:
            out["back_note"] = "2 小時後的穩定資料不足"
    return out


def effective_decoupling(row: dict[str, Any]) -> tuple[float | None, str]:
    """Front-part decoupling when the segments were computed, else TP's summary value."""
    f = ((row.get("seg") or {}).get("front") or {}).get("decoupling_pct")
    if f is not None:
        return f, "前段（PROC-84h）"
    return row.get("decoupling_pct"), "TP 摘要"


# ---------------------------------------------------------------------------
# PROC-85g — FTP tiers
# ---------------------------------------------------------------------------
def ftp_tier(best_w: float | None, ftp: float | None, second_w: float | None = None) -> dict[str, Any]:
    if not best_w or not ftp:
        return {"tier": None, "report": False}
    diff = round(100 * (best_w * 0.95 / ftp - 1), 1)
    lo, hi = FTP_TIERS
    out: dict[str, Any] = {"diff_pct": diff, "x095": round(best_w * 0.95)}
    if diff < lo:
        out.update(tier=f"<{lo:g}%：雜訊內，不提報", report=False)
    elif diff < hi:
        s = round(100 * (second_w * 0.95 / ftp - 1), 1) if second_w else None
        if s is not None and s >= lo:
            out.update(tier=f"{lo:g}–{hi:g}%：第二來源同向（{s:+g}%），提報", report=True, second_diff_pct=s)
        else:
            out.update(tier=f"{lo:g}–{hi:g}%：可能偏低，需第二來源（另一段 ≥12 分硬課／比賽 MMP，或基準測驗）",
                       report=False)
    else:
        out.update(tier=f"≥{hi:g}%：單一證據可提報", report=True)
    return out


# ---------------------------------------------------------------------------
# PROC-84g — EF gates
# ---------------------------------------------------------------------------
def is_quality(title: str | None) -> bool:
    t = str(title or "")
    return any(w in t for w in QUALITY_WORDS) and "基準測驗" not in t


def ef_gates(recent_peak_hr: list[float], base_peak_hr: list[float],
             rpe_latest: float | None, rpe_base: list[float]) -> dict[str, Any]:
    out: dict[str, Any] = {"rule": "PROC-84g"}
    g1 = g2 = None
    if recent_peak_hr and len(base_peak_hr) >= 2:
        drop = round(statistics.mean(base_peak_hr) - statistics.mean(recent_peak_hr), 1)
        g1 = drop < PEAK_HR_DROP_BPM
        out["peak_hr"] = {"recent": round(statistics.mean(recent_peak_hr), 1),
                          "base": round(statistics.mean(base_peak_hr), 1), "drop_bpm": drop, "pass": g1}
    else:
        out["peak_hr"] = {"pass": None, "note": "近窗或前 4 週的間歇課峰值心率不足"}
    if rpe_latest is not None and rpe_base:
        rise = round(rpe_latest - statistics.mean(rpe_base), 1)
        g2 = rise < RPE_RISE
        out["rpe"] = {"latest": rpe_latest, "base": round(statistics.mean(rpe_base), 1), "rise": rise, "pass": g2}
    else:
        out["rpe"] = {"pass": None, "note": "TP 沒填 RPE：看選手留言有無疲勞字眼（人工）"}
    if g1 is False or g2 is False:
        out["verdict"] = "疲勞型 EF 上升：不提報閾值，轉 PER-49"
        out["pass"] = False
    elif g1 and g2:
        out["verdict"] = "兩關通過：列「閾值可能偏低」交 8g-④"
        out["pass"] = True
    else:
        out["verdict"] = "資料不足：缺的那關人工判"
        out["pass"] = None
    return out


# ---------------------------------------------------------------------------
# PER-79b — easy-run HR at the same pace
# ---------------------------------------------------------------------------
def is_easy_run(title: str | None) -> bool:
    t = str(title or "")
    return any(w in t for w in EASY_TITLES) and not any(w in t for w in ("間歇", "節奏", "比賽強度", "長跑"))


def easy_run_index(week: list[dict[str, Any]], base: list[dict[str, Any]],
                   use_power: bool = False) -> dict[str, Any]:
    """Rows: {date, avg_hr, avg_speed_ms, np?, excluded?}. Index = HR ÷ output
    (bigger = more HR for the same pace/power)."""
    key = "np" if use_power else "avg_speed_ms"

    def idx(r: dict[str, Any]) -> float | None:
        hr, o = _num(r.get("avg_hr")), _num(r.get(key))
        return hr / o if hr and o else None

    w = [x for x in (idx(r) for r in week if not r.get("excluded")) if x]
    b = [x for x in (idx(r) for r in base if not r.get("excluded")) if x]
    out: dict[str, Any] = {"rule": "PER-79b", "basis": "心率÷功率" if use_power else "心率÷速度",
                           "n_week": len(w), "n_base": len(b),
                           "excluded": [{"date": r.get("date"), "why": r.get("excluded")}
                                        for r in week + base if r.get("excluded")] or None}
    if len(w) < EASY_MIN_WEEK or len(b) < EASY_MIN_BASE:
        out.update(verdict="資料不足（本週 <2 堂或基準 <4 堂），不判", abnormal=None)
        return out
    mu, sd, wm = statistics.mean(b), statistics.stdev(b), statistics.mean(w)
    out.update(week=round(wm, 3), base_mean=round(mu, 3), base_sd=round(sd, 3),
               z=round((wm - mu) / sd, 2) if sd else None)
    if not use_power:
        spd = statistics.mean([_num(r.get("avg_speed_ms")) for r in base if not r.get("excluded")
                               and _num(r.get("avg_speed_ms"))])
        out["hr_delta_bpm_at_base_pace"] = round((wm - mu) * spd, 1)
    out["abnormal"] = bool(sd and wm > mu + EASY_SD * sd)
    out["verdict"] = "異常（>1 SD）：S3 第 4 項亮" if out["abnormal"] else "正常"
    return out


# ---------------------------------------------------------------------------
# PER-79c — resting HR / HRV
# ---------------------------------------------------------------------------
def wellness_signal(daily: list[dict[str, Any]], end: str) -> dict[str, Any]:
    e = dt.date.fromisoformat(end)
    wk_from, base_from, base_to = e - dt.timedelta(days=6), e - dt.timedelta(days=34), e - dt.timedelta(days=7)

    def vals(key: str, a: dt.date, b: dt.date) -> list[float]:
        out = []
        for r in daily or []:
            try:
                d = dt.date.fromisoformat(str(r.get("date"))[:10])
            except ValueError:
                continue
            v = _num(r.get(key))
            if a <= d <= b and v and v > 0:
                out.append(v)
        return out

    out: dict[str, Any] = {"rule": "PER-79c"}
    rw, rb = vals("rhr", wk_from, e), vals("rhr", base_from, base_to)
    rhr_delta = None
    if len(rw) >= 3 and len(rb) >= 7:
        rhr_delta = round(statistics.mean(rw) - statistics.mean(rb), 1)
        out["rhr"] = {"week_mean": round(statistics.mean(rw), 1), "base_mean": round(statistics.mean(rb), 1),
                      "delta": rhr_delta, "yellow": rhr_delta >= RHR_RISE_BPM}
    else:
        out["rhr"] = {"note": "靜止心率筆數不足（本週 <3 或基準 <7）"}
    hw, hb = vals("hrv", wk_from, e), vals("hrv", base_from, base_to)
    if len(hw) < HRV_MIN_DAYS or len(hb) < 7:
        out["hrv"] = {"note": f"HRV 本週有效 {len(hw)} 天（<{HRV_MIN_DAYS} 不判）或基準不足"}
    else:
        mu, sd, wm = statistics.mean(hb), statistics.stdev(hb), statistics.mean(hw)
        h = {"week_mean": round(wm, 1), "base_mean": round(mu, 1), "base_sd": round(sd, 1),
             "low": wm < mu - HRV_SD_FRAC * sd, "high": wm > mu + HRV_SD_FRAC * sd}
        if h["high"] and rhr_delta is not None and rhr_delta < 0:
            h["saturation_candidate"] = True
            h["note"] = "HRV 升＋靜止心率降：選手主觀疲勞也上升＝紅（副交感飽和，PER-79c）——看回饋"
        out["hrv"] = h
    flags = []
    if (out["rhr"] or {}).get("yellow"):
        flags.append(f"靜止心率 7 日均 +{rhr_delta:g} bpm（黃）")
    if (out.get("hrv") or {}).get("low"):
        flags.append("HRV 週均低於 4 週均 −0.5 SD")
    if (out.get("hrv") or {}).get("saturation_candidate"):
        flags.append("副交感飽和候選（看主觀疲勞）")
    out["flags"] = flags
    return out
