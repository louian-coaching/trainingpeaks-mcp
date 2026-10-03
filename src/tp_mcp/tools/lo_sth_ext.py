"""FORK (羅教練 2026/10/02): StrongTri 提速第二批——預取／改課一步／改課比對／快照。

教練 10/02 裁示 A＋B＋C＋D 全做（排課與改課偏慢）。本檔放 A／B／D 與快照；C 的整週收尾與量級帶
放在 lo_sth.py（lo_sth_calc_loads 的 band、lo_sth_create_week 的 finish）。

  A lo_sth_prep_week       排課前置一次做完：身分→前兩週課表＋活動＋健康＋賽事→質量課逐堂峰值→上週範本 classesJson
  B lo_sth_update_verified 改課一步：文字走 patch（preview→token→寫→讀回比對）、結構走整份替換（dry-run→版本→寫→讀回比對）
  D lo_sth_diff_week       教練改課後比對快照：列出移動／改動的課、結構差異、說明段裡跟著舊值沒改的數字（stale）

快照＝每堂課建立或改完時的 classesJson＋scheduleVersion，存在
~/trainingpeaks-mcp/_scratch/sth_snapshots/<triUserId>/<classScheduleId>.json（TECH-43）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

from tp_mcp.tools import lo_sth_intervals as ivl
from tp_mcp.tools.lo_sth import (
    _default_factory, _dump, _ensure_coach, blocking_of, distance_to_time, find_key, is_ok,
)

CONFIRM_PATCH = "CONFIRM_ASSIGNED_WORKOUT_PATCH"
CONFIRM_UPDATE = "CONFIRM_ASSIGNED_WORKOUT_UPDATE"
AEROBIC_TITLES = ("輕鬆跑", "輕鬆騎", "有氧耐力", "長訓", "休息日", "親子")
POWER_WIN = ("1m", "3m", "5m", "10m", "20m", "30m", "60m")
PACE_DIST = ("400m", "800m", "1km", "3km", "5km", "10km")


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
def snap_root() -> Path:
    env = os.environ.get("LO_STH_SNAPSHOT_DIR")
    return Path(env).expanduser() if env else Path.home() / "trainingpeaks-mcp" / "_scratch" / "sth_snapshots"


def save_snapshot(tri: str, cid: Any, cj: dict[str, Any] | None, date: str | None = None,
                  version: Any = None, title: str | None = None, source: str = "") -> None:
    if cid is None or cj is None:
        return
    d = snap_root() / tri
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.json").write_text(json.dumps({
        "classScheduleId": cid, "date": (date or "")[:10], "title": title or cj.get("title"),
        "scheduleVersion": version, "source": source,
        "savedAt": dt.datetime.now().isoformat(timespec="seconds"), "classes_json": cj,
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def load_snapshot(tri: str, cid: Any) -> dict[str, Any] | None:
    f = snap_root() / tri / f"{cid}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def snapshots_in_range(tri: str, d0: str, d1: str) -> dict[Any, dict[str, Any]]:
    out: dict[Any, dict[str, Any]] = {}
    d = snap_root() / tri
    if d.exists():
        for f in d.glob("*.json"):
            s = json.loads(f.read_text(encoding="utf-8"))
            if d0 <= (s.get("date") or "") <= d1:
                out[s["classScheduleId"]] = s
    return out


# ---------------------------------------------------------------------------
# 讀取小工具
# ---------------------------------------------------------------------------
def iter_week(wk: dict[str, Any]):
    """get_week_schedule → (date, workout)。days 是 list[{date, workouts}]（10/02 實測）。"""
    days = wk.get("days") or []
    if isinstance(days, dict):
        days = [{"date": k, "workouts": v} for k, v in days.items()]
    for day in days:
        for w in day.get("workouts") or []:
            yield str(day.get("date") or w.get("classesDate") or "")[:10], w


def _as_cj(v: Any) -> dict[str, Any] | None:
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v if isinstance(v, dict) else None


async def fetch_cj(cli, tri: str, cid: Any, patch: dict[str, Any] | None = None) -> dict[str, Any]:
    """preview_assigned_workout_update＝免費拿原文＋版本＋token（method/8 §8.6 PLAT-35）。"""
    res = await cli.call("preview_assigned_workout_update", {
        "tri_user_id": tri, "class_schedule_id": cid, "include_classes_json": True,
        "patch_intent": "text_only", "patch_json": json.dumps(patch or {}, ensure_ascii=False),
        "device_resync_policy": "never"})
    plan = res.get("plan") if isinstance(res.get("plan"), dict) else res
    return {"cj": _as_cj(plan.get("afterClassesJson") or plan.get("beforeClassesJson")),
            "version": plan.get("scheduleVersion"), "token": plan.get("previewToken"),
            "target": plan.get("target") or {}, "blocking": blocking_of(res), "raw": res}


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def compact_detail(res: dict[str, Any]) -> dict[str, Any]:
    det = res.get("detail") or {}
    out = {k: det.get(k) for k in ("activityName", "isIndoor", "durationSec", "distanceM", "avgPower",
                                   "normalizedPower", "avgPaceSecPerKm", "avgHeartRate", "maxHeartRate",
                                   "avgCadence", "intensityFactor") if det.get(k) is not None}
    dec = det.get("aerobicDecoupling") or {}
    if dec.get("aerobicDecouplingPct") is not None:
        out["decouplingPct"] = dec["aerobicDecouplingPct"]
    peaks = {}
    for sport, metrics in ((res.get("peakSummary") or {}).get("cleanPeakCurve") or {}).items():
        for m, arr in metrics.items():
            keep = PACE_DIST if m in ("paceDistance", "speedDistance") else POWER_WIN
            vals = {x["window"]: x["value"] for x in arr if x.get("window") in keep}
            if vals:
                peaks[m] = vals
    out["peaks"] = peaks
    return out


def _fmt_pace(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"



# ---------------------------------------------------------------------------
# 結構完整性（10/02 優化 1／3／4）
# ---------------------------------------------------------------------------
# 本機負荷估算＝tp-ai-layer/tools/sth_build.py 的 _EST（4 位選手 22 堂官方值擬合，單堂 ±10%）
_EST = {"CYCLE": (1.624, 2.6), "RUN": (1.579, 3.9)}


def _hms(sec: int) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _hms_to_s(v: Any) -> int | None:
    try:
        h, m, x = (int(float(p)) for p in str(v).split(":"))
        return h * 3600 + m * 60 + x
    except (ValueError, TypeError):
        return None


def struct_seconds(cj: dict[str, Any] | None) -> int:
    """各 stage（段秒數總和 × times）加總＝這堂課真正的長度。"""
    tot = 0
    for st in (cj or {}).get("stages") or []:
        tot += sum(int(_num(s.get("targetSeconds"))) for s in st.get("sections") or []) * int(st.get("times") or 1)
    return tot


def est_stl(cj: dict[str, Any] | None) -> int:
    cj = cj or {}
    sp = "CYCLE" if cj.get("sportType") in ("CYCLE", "RIDE") else cj.get("sportType")
    if sp not in _EST or not cj.get("stages"):
        return int(_num(cj.get("stl")))
    a, k = _EST[sp]
    tot = 0.0
    for st in cj["stages"]:
        for x in st.get("sections") or []:
            r = x.get("thresholdFtpRange") or x.get("thresholdSpeedRange") or [0, 0]
            tot += (_num(x.get("targetSeconds")) * int(st.get("times") or 1) / 3600.0 * 100 * a
                    * ((_num(r[0]) + _num(r[1])) / 200.0) ** k)
    return int(round(tot))


def integrity(cj: dict[str, Any] | None, week_duration_min: Any = None) -> list[dict[str, Any]]:
    """教練在 STH 介面改完常留下的自相矛盾（10/02 胡逸凡 99218 實例）：
    ①距離段 targetSeconds 換算出的配速離區間太遠（4 公里寫 600 秒＝2:30/km）
    ②durationSeconds／duration／timeline 跟各段加總對不上（週課表時長讀 durationSeconds）。"""
    out: list[dict[str, Any]] = []
    if not cj or not cj.get("stages"):
        return out
    for si, st in enumerate(cj["stages"]):
        for ci, x in enumerate(st.get("sections") or []):
            num = x.get("thresholdSpeedRangeNum")
            if x.get("capacity") == "distance" and num and _num(x.get("targetDistance")) > 0 and _num(num[0]) > 0:
                km, secs = _num(x["targetDistance"]), _num(x.get("targetSeconds"))
                slow, fast = max(_num(num[0]), _num(num[1])), min(_num(num[0]), _num(num[1]))
                implied = secs / km if km else 0
                if not (fast * 0.85 <= implied <= slow * 1.15):
                    fix = int(round(km * (slow + fast) / 2))
                    out.append({"code": "IMPLAUSIBLE_TARGET_SECONDS", "stage": si, "section": ci,
                                "message": (f"{km:g} 公里寫 {int(secs)} 秒＝{_fmt_pace(implied)}/km，"
                                            f"區間是 {_fmt_pace(slow)}~{_fmt_pace(fast)}"),
                                "fix": {"targetSeconds": fix}})
    total = struct_seconds(cj)
    if total:
        ds = cj.get("durationSeconds")
        if ds is not None and abs(int(_num(ds)) - total) > 30:
            out.append({"code": "DURATION_SECONDS_STALE",
                        "message": f"durationSeconds={int(_num(ds))}，各段加總 {total}（週課表時長讀這個）",
                        "fix": {"durationSeconds": total}})
        hs = _hms_to_s(cj.get("duration"))
        if hs is not None and abs(hs - total) > 30:
            out.append({"code": "DURATION_STR_STALE", "message": f"duration={cj.get('duration')}，各段加總 {_hms(total)}",
                        "fix": {"duration": _hms(total)}})
        tl = cj.get("timeline") or []
        if tl and len(tl) == len(cj["stages"]):
            for i, (t, st) in enumerate(zip(tl, cj["stages"])):
                want = sum(int(_num(x.get("targetSeconds"))) for x in st.get("sections") or []) * int(st.get("times") or 1)
                if abs(int(_num(t.get("duration"))) - want) > 30:
                    out.append({"code": "TIMELINE_STALE", "stage": i,
                                "message": f"timeline[{i}]={int(_num(t.get('duration')))}，該段應為 {want}"})
        if week_duration_min is not None and abs(_num(week_duration_min) * 60 - total) > 60:
            out.append({"code": "WEEK_VIEW_DURATION_STALE",
                        "message": f"週課表顯示 {week_duration_min} 分，結構是 {round(total / 60, 1)} 分"})
    return out


def sync_durations(cj: dict[str, Any]) -> list[str]:
    """由各段秒數回填 timeline、durationSeconds、duration（10/02 優化 1）。回傳改了哪些欄位。"""
    changed: list[str] = []
    if not cj.get("stages"):
        return changed
    tl = cj.get("timeline") or []
    if tl and len(tl) == len(cj["stages"]):
        for t, st in zip(tl, cj["stages"]):
            secs = [int(_num(x.get("targetSeconds"))) for x in st.get("sections") or []]
            want = sum(secs) * int(st.get("times") or 1)
            if int(_num(t.get("duration"))) != want:
                t["duration"] = want
                changed.append("timeline")
            stl_ = t.get("stageTimeline") or []
            if len(stl_) == len(secs):
                for x, sec in zip(stl_, secs):
                    if int(_num(x.get("duration"))) != sec:
                        x["duration"] = sec
                        changed.append("timeline")
    total = struct_seconds(cj)
    if total:
        if int(_num(cj.get("durationSeconds"))) != total:
            cj["durationSeconds"] = total
            changed.append("durationSeconds")
        if cj.get("duration") != _hms(total):
            cj["duration"] = _hms(total)
            changed.append("duration")
    return sorted(set(changed))


def apply_integrity_fix(cj: dict[str, Any]) -> dict[str, Any]:
    """照 integrity 的 fix 修好：先修距離段秒數，再由各段回填 timeline／durationSeconds／duration。"""
    import copy as _c
    c = _c.deepcopy(cj)
    for it in integrity(c):
        if it["code"] == "IMPLAUSIBLE_TARGET_SECONDS":
            c["stages"][it["stage"]]["sections"][it["section"]]["targetSeconds"] = it["fix"]["targetSeconds"]
    sync_durations(c)
    return c


def load_sanity(before: dict[str, Any], cj: dict[str, Any], load_fallback: str = "warn") -> list[dict[str, Any]]:
    """優化 3：官方負荷合理性。結構變了、官方 STL 卻一模一樣＝官方沒算到改的那段
    （10/02 99218：暖身 2→4 公里仍回 54）。load_fallback="scale" 時按本機估算比例改用建議值。"""
    w: list[dict[str, Any]] = []
    eb, ea = est_stl(before), est_stl(cj)
    b_stl, stl = int(_num(before.get("stl"))), int(_num(cj.get("stl")))
    if before and _stages_sig(before) != _stages_sig(cj) and b_stl and stl == b_stl and eb and abs(ea - eb) >= 3:
        sug = int(round(b_stl * ea / eb))
        w.append({"code": "LOAD_UNCHANGED_AFTER_STRUCTURE_CHANGE", "official_stl": stl, "estimate_before": eb,
                  "estimate_after": ea, "suggested_stl": sug, "applied": load_fallback == "scale"})
        if load_fallback == "scale":
            cj["sth"] = int(round(_num(cj.get("sth")) * sug / max(stl, 1)))
            cj["stl"] = sug
    elif ea and stl and abs(stl - ea) / ea > 0.25:
        w.append({"code": "LOAD_FAR_FROM_ESTIMATE", "official_stl": stl, "estimate": ea})
    return w

# ---------------------------------------------------------------------------
# A：排課前置一次做完
# ---------------------------------------------------------------------------
def unplanned_flags(extra: list[dict[str, Any]], plan_rows: list[dict[str, Any]], week_start: str) -> list[str]:
    """10/02 優化 7：沒排的大量訓練＝紅旗（只上報，週型教練決定）。"""
    flags: list[str] = []
    longest = max([_num(r.get("plan_min")) for r in plan_rows] or [0])
    by_day: dict[str, float] = {}
    for e in extra:
        m = _num(e.get("min"))
        by_day[e.get("date") or ""] = by_day.get(e.get("date") or "", 0) + m
        if m >= 90 or (longest and m >= longest):
            flags.append(f"{e.get('date')} 未排 {e.get('sport')} {e.get('title')} {round(m)} 分（≥90 分或超過最長計畫課）")
    for d, m in sorted(by_day.items()):
        if m >= 180:
            flags.append(f"{d} 未排訓練合計 {round(m)} 分（≥3 小時）")
        if d and _d(d) >= _d(week_start) - dt.timedelta(days=3) and m >= 60:
            flags.append(f"{d} 距新週 ≤3 天有未排訓練 {round(m)} 分")
    for r in plan_rows:
        if r.get("done") is False and any(t in (r.get("title") or "") for t in ("長距離", "長跑", "長騎", "換項跑")):
            flags.append(f"{r['date']} 漏了長課／換項跑：{r['title']}")
    return list(dict.fromkeys(flags))


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _iv_line(d: dict[str, Any]) -> str | None:
    iv = d.get("intervals") or {}
    if not iv.get("blocks"):
        return None
    parts = []
    for b in iv["blocks"]:
        val = f"{b['avg_w']}W" if "avg_w" in b else b.get("pace")
        parts.append(f"#{b['n']} {round(b['sec'] / 60, 1)}分 {val} 心率{b.get('avg_hr')}/{b.get('max_hr')}")
    return f"{d.get('activityName')}：" + "｜".join(parts) + ("" if iv.get("match") else f"（計畫 {iv.get('planned_blocks')} 段、抓到 {iv.get('detected_blocks')}）")


async def lo_sth_prep_week(tri_user_id: str, week_start: str, save_dir: str, history_days: int = 14,
                           template_days: int = 7, client_factory=_default_factory,
                           intervals: bool = True) -> dict[str, Any]:
    ws = _d(week_start)
    h0, we = ws - dt.timedelta(days=history_days), ws + dt.timedelta(days=6)
    sd = Path(os.path.expanduser(save_dir))
    sd.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"tri_user_id": tri_user_id, "week": [str(ws), str(we)], "history_from": str(h0)}
    async with client_factory() as cli:
        out["identity"] = await _ensure_coach(cli)
        wk = await cli.call("get_week_schedule", {"tri_user_id": tri_user_id, "date_from": str(h0), "date_to": str(we)})
        got = find_key(wk, "triUserId")
        if got and got != tri_user_id:  # 黄帆實例：不吃 athlete_ref 時會靜默回教練本人
            return {**out, "isError": True, "error_code": "TRI_MISMATCH", "message": f"讀回 triUserId={got}，與要求不符，停止"}
        acts = await cli.call("list_recent_activities", {"tri_user_id": tri_user_id, "days": max(45, history_days + 7)})
        health = await cli.call("get_health_metrics", {"tri_user_id": tri_user_id, "days": 14})
        races = await cli.call("get_upcoming_races", {"tri_user_id": tri_user_id})

        # 活動：同一筆裝置＋手動只留裝置那筆
        dev, seen = [], set()
        for a in sorted(acts.get("activities") or [], key=lambda x: x.get("sourceType") != "device"):
            key = (a.get("date"), a.get("manualActivityId") or a.get("activityId"))
            if key in seen:
                continue
            seen.add(key)
            dev.append(a)
        by_sched: dict[Any, list] = {}
        for a in dev:
            if a.get("classScheduleId"):
                by_sched.setdefault(a["classScheduleId"], []).append(a)

        today = dt.date.today()
        plan_rows, quality_ids, tpl = [], [], []
        upcoming = []
        for date, w in iter_week(wk):
            cid = w.get("classScheduleId")
            row = {"date": date, "id": cid, "sport": w.get("sportType"), "title": w.get("title"),
                   "plan_min": w.get("durationMin"), "plan_stl": w.get("plannedSTL")}
            if _d(date) >= ws:
                upcoming.append(row)
                continue
            if w.get("sportType") == "REST":
                row["done"] = "rest"
                plan_rows.append(row)
                continue
            got_acts = by_sched.get(cid, [])
            if got_acts:
                a = got_acts[0]
                row.update({"done": True, "act_id": a.get("activityId"), "act_min": a.get("durationMin"),
                            "act_km": a.get("distanceKm"), "act_power": a.get("avgPower"),
                            "act_pace": _fmt_pace(a["avgPaceSecPerKm"]) if a.get("avgPaceSecPerKm") else None,
                            "act_swim_pace": a.get("avgPacePer100mSec"), "completion": a.get("completionPercent"),
                            "act_title": a.get("title")})
                if (w.get("sportType") in ("RUN", "CYCLE") and a.get("activityId")
                        and not any(t in (w.get("title") or "") for t in AEROBIC_TITLES[:3])):
                    quality_ids.append((cid, a["activityId"]))
            else:
                row["done"] = False if _d(date) < today else None
            plan_rows.append(row)
            if _d(date) >= ws - dt.timedelta(days=template_days) and w.get("sportType") not in (None,):
                tpl.append((date, w))

        extra = [{"date": a.get("date"), "sport": a.get("sportType"), "title": a.get("title"),
                  "min": a.get("durationMin"), "km": a.get("distanceKm"), "power": a.get("avgPower")}
                 for a in dev if not a.get("classScheduleId") and a.get("date", "") >= str(h0)]
        swims = sorted(a.get("date") for a in dev if a.get("sportType") == "SWIM")

        details = {}
        for cid, aid in quality_ids:
            r = await cli.call("get_activity_detail", {"tri_user_id": tri_user_id, "activity_id": str(aid)})
            details[str(cid)] = compact_detail(r)
            if intervals:  # 10/02 優化 7：逐段驗收（STH 沒有 lap，用逐秒時序重建）
                try:
                    full = await cli.call("get_activity_detail", {"tri_user_id": tri_user_id,
                                                                  "activity_id": str(aid), "full": True})
                    plan = (await fetch_cj(cli, tri_user_id, cid))["cj"] or {}
                    details[str(cid)]["intervals"] = ivl.analyze(ivl.series(full), plan)
                except Exception as e:  # noqa: BLE001 — 逐段只是加值，失敗不擋前置
                    details[str(cid)]["intervals"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}

        templates = {}
        for date, w in tpl:
            cid = w.get("classScheduleId")
            if w.get("sportType") in ("SWIM",) and "長訓" in (w.get("title") or ""):
                continue
            got = await fetch_cj(cli, tri_user_id, cid)
            if got["cj"]:
                f = sd / f"tpl_{cid}.json"
                f.write_text(json.dumps(got["cj"], ensure_ascii=False, indent=1), encoding="utf-8")
                templates[str(cid)] = {"date": date, "title": w.get("title"), "file": str(f)}
                save_snapshot(tri_user_id, cid, got["cj"], date, got["version"], w.get("title"), "prep")

        # FORK (2026/10/03): STH's completionPercent is time-based; method/0 原則五
        # judges swims by metres (程亮 09/29: 123.6% by time, 2100 m swum).
        from tp_mcp.tools.lo_review import planned_distance_m
        for row in plan_rows:
            if row.get("sport") != "SWIM" or row.get("done") is not True:
                continue
            row["completion_basis"] = "time"
            act_km = row.get("act_km")
            if not isinstance(act_km, (int, float)) or act_km <= 0:
                continue
            try:
                tf = templates.get(str(row.get("id")))
                cj = (json.loads(Path(tf["file"]).read_text(encoding="utf-8")) if tf
                      else (await fetch_cj(cli, tri_user_id, row.get("id")))["cj"]) or {}
            except Exception:  # noqa: BLE001 — 只是加值，失敗不擋前置
                continue
            plan_m = planned_distance_m(cj.get("summary"))
            if plan_m:
                act_m = round(act_km * 1000)
                row.update({"plan_m": plan_m, "act_m": act_m,
                            "done_pct": round(100.0 * act_m / plan_m, 1), "completion_basis": "metres"})

    hdaily = [{k: r.get(k) for k in ("date", "hrv", "rhr", "sleepHours") if r.get(k) is not None}
              for r in (health.get("daily") or [])]
    digest = {**out, "plan_vs_actual": plan_rows, "unplanned_activities": extra,
              "last_swim": swims[-1] if swims else None, "quality_details": details,
              "templates": templates, "next_week_existing": upcoming,
              "health_daily": [h for h in hdaily if len(h) > 1], "races": races.get("races") or [],
              "health_device": health.get("deviceName")}
    out["digest_file"] = _dump(str(sd / "prep.json"), digest)
    done = [r for r in plan_rows if r.get("done")]
    missed = [f"{r['date']} {r['title']}" for r in plan_rows if r.get("done") is False]
    out.update({
        "summary": {"planned_past": len(plan_rows), "done": len(done), "missed": missed,
                    "unplanned": [f"{e['date']} {e['sport']} {e['title']} {e['min']}分" for e in extra],
                    "last_swim": digest["last_swim"], "races": len(digest["races"]),
                    "quality_detail_count": len(details), "templates": len(templates),
                    "red_flags": unplanned_flags(extra, plan_rows, week_start),
                    "intervals": [x for x in (_iv_line(v) for v in details.values()) if x],
                    "next_week_existing": [f"{u['date']} {u['title']}" for u in upcoming]},
        "next": "讀 digest_file 的 plan_vs_actual／quality_details 寫前週回顧；建課直接抄 templates 的檔"})
    return out


# ---------------------------------------------------------------------------
# B：改課一步（讀回比對）
# ---------------------------------------------------------------------------
def _norm(s: Any) -> str:
    return "\n".join(l.rstrip() for l in str(s or "").strip().splitlines())


def _stages_sig(cj: dict[str, Any] | None) -> list:
    sig = []
    for st in (cj or {}).get("stages") or []:
        sig.append([st.get("times"), [(s.get("stageMode"), s.get("capacity"), s.get("targetDistance") if s.get("capacity") == "distance" else s.get("targetSeconds"),
                                       s.get("thresholdFtpRange") or s.get("thresholdSpeedRange")) for s in st.get("sections") or []]])
    return sig


async def _refs_for(cli, tri: str, cid: Any, date: str) -> tuple[Any, Any]:
    wk = await cli.call("get_week_schedule", {"tri_user_id": tri, "date_from": date, "date_to": date})
    for _, w in iter_week(wk):
        if w.get("classScheduleId") == cid:
            return w.get("scheduleRef") or w.get("schedule_ref"), w.get("athleteRef")
    return None, None


async def lo_sth_update_verified(tri_user_id: str, class_schedule_id: Any, text: dict[str, str] | None = None,
                                 classes_json_file: str | None = None, duration_min: int | None = None,
                                 recalc_load: bool = False, dry_run: bool = False, idempotency_key: str | None = None,
                                 client_factory=_default_factory, sleep=asyncio.sleep,
                                 load_fallback: str = "warn") -> dict[str, Any]:
    cid = int(class_schedule_id)
    key = idempotency_key or f"lo-sth-{cid}-{dt.datetime.now().strftime('%m%d%H%M%S')}"
    if bool(text) == bool(classes_json_file):
        return {"isError": True, "error_code": "BAD_ARGS", "message": "text 與 classes_json_file 擇一（純文字走 patch，改結構或負荷走整份替換，PLAT-29）"}
    out: dict[str, Any] = {"classScheduleId": cid, "mode": "text_patch" if text else "replace", "dry_run": dry_run}
    async with client_factory() as cli:
        if text:
            bad = set(text) - {"summary", "trainingAdvice", "title"}
            if bad:
                return {**out, "isError": True, "error_code": "BAD_FIELDS", "message": f"text 只收 summary／trainingAdvice／title：{sorted(bad)}"}
            pv = await fetch_cj(cli, tri_user_id, cid, text)
            if pv["blocking"] or not pv["token"]:
                return {**out, "isError": True, "error_code": "PREVIEW_BLOCKED", "blocking": pv["blocking"][:5]}
            if dry_run:
                return {**out, "ok": True, "would_change": sorted(text), "version": pv["version"]}
            res = await cli.call("update_assigned_workout_patch", {
                "tri_user_id": tri_user_id, "class_schedule_id": cid, "patch_intent": "text_only",
                "patch_json": json.dumps(text, ensure_ascii=False), "device_resync_policy": "never",
                "preview_token": pv["token"], "expected_version": pv["version"], "dry_run": False,
                "confirm_write": True, "confirm_token": CONFIRM_PATCH, "idempotency_key": key})
            if not is_ok(res):
                return {**out, "isError": True, "error_code": "WRITE_FAILED", "raw_head": json.dumps(res, ensure_ascii=False)[:400]}
            after = await fetch_cj(cli, tri_user_id, cid)
            mism = [k for k, v in text.items() if _norm((after["cj"] or {}).get(k)) != _norm(v)]
            same_struct = _stages_sig(after["cj"]) == _stages_sig(pv["cj"])
            out.update({"ok": not mism and same_struct, "fields_verified": sorted(set(text) - set(mism)),
                        "mismatch": mism, "structure_untouched": same_struct, "version": after["version"]})
        else:
            cj = json.loads(Path(os.path.expanduser(classes_json_file)).read_text(encoding="utf-8"))
            before = await fetch_cj(cli, tri_user_id, cid)
            date = str(before["target"].get("classesDate") or "")[:10]
            warnings: list[dict[str, Any]] = []
            # 優化 1：durationSeconds／duration／timeline 一律由各段回填（週課表時長讀 durationSeconds）
            synced = sync_durations(cj)
            if synced:
                warnings.append({"code": "DURATIONS_SYNCED", "fields": synced, "durationSeconds": cj.get("durationSeconds")})
            warnings += integrity(cj)
            if recalc_load and cj.get("stages"):
                sport = "CYCLE" if cj.get("sportType") in ("CYCLE", "RIDE") else "RUN"
                r = await cli.call("calculate_workout_sth", {
                    "classes_json": json.dumps(distance_to_time(cj), ensure_ascii=False, separators=(",", ":")),
                    "sport_type": sport, "tri_user_id": tri_user_id})
                sth, stl = find_key(r, "sth"), find_key(r, "stl")
                if isinstance(sth, (int, float)) and sth > 0:
                    cj["sth"], cj["stl"] = int(round(sth)), int(round(stl))
                    warnings += load_sanity(before["cj"] or {}, cj, load_fallback)
            sref, aref = await _refs_for(cli, tri_user_id, cid, date)
            if not sref:
                return {**out, "isError": True, "error_code": "NO_SCHEDULE_REF", "message": f"{date} 找不到 {cid} 的 scheduleRef"}
            total = struct_seconds(cj)
            dur = int(duration_min if duration_min is not None else round((total or int(_num(cj.get("durationSeconds")))) / 60))
            if duration_min is not None and total and abs(duration_min - total / 60) > 1:
                warnings.append({"code": "DURATION_MIN_MISMATCH", "duration_min": duration_min,
                                 "structure_min": round(total / 60, 1)})
            base = {"tri_user_id": tri_user_id, "schedule_ref": sref, "athlete_ref": aref, "duration_min": dur,
                    "new_classes_json": json.dumps(cj, ensure_ascii=False, separators=(",", ":"))}
            dry = await cli.call("update_assigned_workout", {**base, "dry_run": True})
            if blocking_of(dry) or dry.get("isError"):
                return {**out, "isError": True, "error_code": "DRY_RUN_BLOCKED", "blocking": blocking_of(dry)[:5],
                        "raw_head": json.dumps(dry, ensure_ascii=False)[:300]}
            ver = find_key(dry, "expectedVersion")
            if ver is None:
                ver = find_key(dry, "beforeVersion")
            if dry_run:
                return {**out, "ok": True, "expected_version": ver, "sth": cj.get("sth"), "stl": cj.get("stl")}
            res = await cli.call("update_assigned_workout", {**base, "dry_run": False, "confirm_write": True,
                                                              "confirm_token": CONFIRM_UPDATE, "expected_version": int(ver or 0),
                                                              "idempotency_key": key})
            matches = find_key(res, "classesJsonMatches")
            after = await fetch_cj(cli, tri_user_id, cid)
            struct_ok = _stages_sig(after["cj"]) == _stages_sig(cj)
            text_ok = all(_norm((after["cj"] or {}).get(k)) == _norm(cj.get(k)) for k in ("summary", "trainingAdvice", "title"))
            # 優化 1：驗收看週課表（使用者看到的那一層），不是只看自己送出的 duration_min
            wv = await cli.call("get_week_schedule", {"tri_user_id": tri_user_id, "date_from": date, "date_to": date})
            row = next((w for _, w in iter_week(wv) if w.get("classScheduleId") == cid), {}) or {}
            wk_min, wk_stl = row.get("durationMin"), row.get("plannedSTL")
            week_ok = (wk_min is not None and (not total or abs(_num(wk_min) * 60 - total) <= 60)
                       and (wk_stl is None or int(_num(wk_stl)) == int(_num(cj.get("stl")))))
            out.update({"ok": bool(matches) and struct_ok and text_ok and week_ok, "classesJsonMatches": matches,
                        "structure_verified": struct_ok, "text_verified": text_ok, "sth": cj.get("sth"),
                        "stl": cj.get("stl"), "duration_min": dur, "version": after["version"],
                        "week_view": {"durationMin": wk_min, "plannedSTL": wk_stl, "ok": week_ok},
                        **({"warnings": warnings} if warnings else {})})
            if not is_ok(res):
                out.update({"isError": True, "error_code": "WRITE_FAILED", "raw_head": json.dumps(res, ensure_ascii=False)[:400]})
        if out.get("ok") and after.get("cj"):
            save_snapshot(tri_user_id, cid, after["cj"], str(after["target"].get("classesDate") or ""),
                          after["version"], after["cj"].get("title"), "update_verified")
    return out


# ---------------------------------------------------------------------------
# D：教練改課後比對
# ---------------------------------------------------------------------------
def _fmt_dur(sec: Any) -> str | None:
    if not isinstance(sec, (int, float)) or sec <= 0:
        return None
    sec = int(sec)
    if sec < 60:
        return f"{sec}秒"
    m, s = divmod(sec, 60)
    if m >= 60 and not s:
        h, mm = divmod(m, 60)
        return f"{h}小時" + (f"{mm}分" if mm else "")
    return f"{m}分{s}秒" if s else f"{m}分鐘"


def _fmt_dist(km: Any) -> str | None:
    if not isinstance(km, (int, float)) or km <= 0:
        return None
    return f"{int(round(km * 1000))}公尺" if km < 1 else (f"{km:g}公里")


def _section_tokens(s: dict[str, Any]) -> dict[str, str]:
    """一段 section 在課表文字裡會長成的樣子（瓦數／配速／時間／距離）。"""
    t: dict[str, str] = {}
    w = s.get("thresholdFtpRangeNum")
    if isinstance(w, list) and len(w) == 2 and s.get("thresholdFtpRange"):
        t["power"] = f"{w[0]}~{w[1]}W"
    p = s.get("thresholdSpeedRangeNum")
    if isinstance(p, list) and len(p) == 2 and s.get("thresholdSpeedRange"):
        t["pace"] = f"{_fmt_pace(p[0])}~{_fmt_pace(p[1])}"
    if s.get("capacity") == "distance":
        d = _fmt_dist(s.get("targetDistance"))
        if d:
            t["dist"] = d
    else:
        d = _fmt_dur(s.get("targetSeconds"))
        if d:
            t["dur"] = d
    return t


def _flat(cj: dict[str, Any] | None) -> list[tuple[int, int, dict[str, Any]]]:
    return [(si, ci, s) for si, st in enumerate((cj or {}).get("stages") or [])
            for ci, s in enumerate(st.get("sections") or [])]


def stale_tokens(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[dict[str, Any]]:
    """結構變了、但文字裡還留著舊值的地方（PROC-80b：以圖形為準）。"""
    text = "\n".join(str((new or {}).get(k) or "") for k in ("summary", "trainingAdvice"))

    def has(tok: str | None) -> bool:
        # 前面不能緊貼數字／~／.（避免「10~15分鐘」的 15分鐘、「110分鐘」的 10分鐘被誤判）
        return bool(tok) and re.search(r"(?<![\d~.])" + re.escape(tok), text) is not None

    out = []
    fo, fn = _flat(old), _flat(new)
    for i, (si, ci, so) in enumerate(fo):
        sn = fn[i][2] if i < len(fn) else None
        to = _section_tokens(so)
        tn = _section_tokens(sn) if sn else {}
        for kind, val in to.items():
            nv = tn.get(kind)
            if nv == val:
                continue
            out.append({"stage": si, "section": ci, "kind": kind, "old": val, "new": nv,
                        "removed": sn is None, "old_value_still_in_text": has(val),
                        "new_value_in_text": has(nv)})
    # 新增的段（新結構比較長）
    for i in range(len(fo), len(fn)):
        si, ci, sn = fn[i]
        out.append({"stage": si, "section": ci, "kind": "added", "old": None,
                    "new": " ".join(_section_tokens(sn).values()), "removed": False, "old_value_still_in_text": False,
                    "new_value_in_text": False})
    # 段數相同但 times 變了
    for si, (a, b) in enumerate(zip((old or {}).get("stages") or [], (new or {}).get("stages") or [])):
        if a.get("times") != b.get("times"):
            out.append({"stage": si, "section": None, "kind": "times", "old": f"{a.get('times')}組", "new": f"{b.get('times')}組",
                        "removed": False, "old_value_still_in_text": has(f"{a.get('times')}組")})
    return out


def text_diff(old: Any, new: Any, limit: int = 20) -> list[str]:
    """優化 5：文字欄位改了什麼，直接給 unified diff（只留 +／- 行）。"""
    import difflib
    lines = [l for l in difflib.unified_diff(_norm(old).splitlines(), _norm(new).splitlines(), lineterm="", n=0)
             if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return lines[:limit] + ([f"…另有 {len(lines) - limit} 行"] if len(lines) > limit else [])


def structure_diff(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """優化 5：只列有變的段（第幾 stage／section、改前改後），不再整份印兩遍。"""
    out: list[dict[str, Any]] = []
    so, sn = (old or {}).get("stages") or [], (new or {}).get("stages") or []
    for si in range(max(len(so), len(sn))):
        a = so[si] if si < len(so) else None
        b = sn[si] if si < len(sn) else None
        if a is None or b is None:
            out.append({"stage": si, "old": _stages_sig({"stages": [a]})[0] if a else None,
                        "new": _stages_sig({"stages": [b]})[0] if b else None})
            continue
        if a.get("times") != b.get("times"):
            out.append({"stage": si, "times": [a.get("times"), b.get("times")]})
        sa, sb = _stages_sig({"stages": [a]})[0][1], _stages_sig({"stages": [b]})[0][1]
        for ci in range(max(len(sa), len(sb))):
            x = sa[ci] if ci < len(sa) else None
            y = sb[ci] if ci < len(sb) else None
            if x != y:
                out.append({"stage": si, "section": ci, "old": x, "new": y})
    return out


def week_stl(wk: dict[str, Any]) -> dict[str, int]:
    by: dict[str, int] = {}
    for _, w in iter_week(wk):
        sp = "RIDE" if w.get("sportType") in ("CYCLE", "RIDE") else str(w.get("sportType"))
        by[sp] = by.get(sp, 0) + int(_num(w.get("plannedSTL")))
    by["three_sport"] = sum(v for k, v in by.items() if k in ("RUN", "RIDE", "SWIM"))
    return by


async def lo_sth_diff_week(tri_user_id: str, date_from: str, date_to: str, accept: bool = False,
                           save_to: str | None = None, client_factory=_default_factory,
                           band: list[int] | None = None, band_sports: list[str] | None = None) -> dict[str, Any]:
    snaps = snapshots_in_range(tri_user_id, date_from, date_to)
    rows, seen = [], set()
    fix_dir = Path(os.path.expanduser(save_to)).parent if save_to else None
    async with client_factory() as cli:
        wk = await cli.call("get_week_schedule", {"tri_user_id": tri_user_id, "date_from": date_from, "date_to": date_to})
        for date, w in iter_week(wk):
            cid = w.get("classScheduleId")
            seen.add(cid)
            snap = load_snapshot(tri_user_id, cid)
            if snap is None:
                # 優化 5：教練新加的課直接附內容，不必另外抓
                got = await fetch_cj(cli, tri_user_id, cid)
                cj = got["cj"] or {}
                row = {"id": cid, "date": date, "title": w.get("title"), "status": "no_snapshot",
                       "sport": w.get("sportType"), "durationMin": w.get("durationMin"), "plannedSTL": w.get("plannedSTL"),
                       "summary": _norm(cj.get("summary"))[:400], "trainingAdvice_head": _norm(cj.get("trainingAdvice"))[:200]}
                integ = integrity(cj, w.get("durationMin"))
                if integ:
                    row["integrity"] = integ
                rows.append(row)
                if accept:
                    save_snapshot(tri_user_id, cid, cj, date, got["version"], w.get("title"), "diff_accept")
                continue
            ver = w.get("scheduleVersion")
            moved = snap.get("date") and snap["date"] != date
            if ver and ver == snap.get("scheduleVersion") and not moved:
                continue
            got = await fetch_cj(cli, tri_user_id, cid)
            old, new = snap.get("classes_json") or {}, got["cj"] or {}
            changes: dict[str, Any] = {}
            if moved:
                changes["moved"] = [snap["date"], date]
            for k in ("title", "summary", "trainingAdvice"):
                if _norm(old.get(k)) != _norm(new.get(k)):
                    changes[k] = text_diff(old.get(k), new.get(k))
            if _stages_sig(old) != _stages_sig(new):
                changes["structure"] = structure_diff(old, new)
            for k in ("stl", "durationSeconds", "duration"):
                if old.get(k) != new.get(k):
                    changes[k] = [old.get(k), new.get(k)]
            stale = stale_tokens(old, new) if "structure" in changes else []
            if not changes:
                if accept:
                    save_snapshot(tri_user_id, cid, new, date, got["version"], w.get("title"), "diff_accept")
                continue
            row = {"id": cid, "date": date, "title": w.get("title"), "status": "changed",
                   "changes": changes, "stale_text": stale,
                   "text_only": "structure" not in changes and not moved}
            # 優化 4：教練介面改完留下的矛盾（距離段秒數、時長欄位）；有 save_to 就順手寫出修好的檔
            integ = integrity(new, w.get("durationMin"))
            if integ:
                row["integrity"] = integ
                if fix_dir and any(i.get("fix") for i in integ):
                    fixed = apply_integrity_fix(new)
                    f = fix_dir / f"fix_{cid}.json"
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(json.dumps(fixed, ensure_ascii=False), encoding="utf-8")
                    row["fix_file"] = str(f)
                    row["fix_duration_min"] = round(struct_seconds(fixed) / 60)
            rows.append(row)
            if accept:
                save_snapshot(tri_user_id, cid, new, date, got["version"], w.get("title"), "diff_accept")
        for cid, s in snaps.items():
            if cid not in seen:
                rows.append({"id": cid, "date": s.get("date"), "title": s.get("title"), "status": "deleted"})
    by = week_stl(wk)
    from tp_mcp.tools.lo_sth import band_check
    out = {"tri_user_id": tri_user_id, "range": [date_from, date_to],
           "changed": [r for r in rows if r["status"] == "changed"],
           "deleted": [r for r in rows if r["status"] == "deleted"],
           "new": [r for r in rows if r["status"] == "no_snapshot"],
           "no_snapshot": [r["id"] for r in rows if r["status"] == "no_snapshot"],
           "stale_total": sum(len([x for x in r.get("stale_text", []) if x.get("old_value_still_in_text")]) for r in rows),
           "integrity_total": sum(len(r.get("integrity") or []) for r in rows),
           "week_stl": by,
           **({"band": band_check({k: v for k, v in by.items() if k != "three_sport"}, band, band_sports)} if band else {}),
           "accepted": accept,
           "next": ("stale_text 裡 old_value_still_in_text=true＝文字還留著舊值：照 PROC-80b 以圖形為準，用 "
                    "lo_sth_update_verified(text=…) 改；integrity 有 fix_file＝平台欄位自相矛盾（多半是時長顯示錯），"
                    "用 lo_sth_update_verified(classes_json_file=fix_file, duration_min=fix_duration_min) 修（屬改教練的課，先問）；"
                    "都處理完 accept=true 收快照")}
    if save_to:
        out["saved_to"] = _dump(save_to, {**out, "rows": rows})
    return out


async def lo_sth_verify_intervals(tri_user_id: str, activity_id: str, class_schedule_id: Any = None,
                                  classes_json_file: str | None = None, work_pct: float = 85.0,
                                  save_to: str | None = None, client_factory=_default_factory) -> dict[str, Any]:
    async with client_factory() as cli:
        full = await cli.call("get_activity_detail", {"tri_user_id": tri_user_id, "activity_id": str(activity_id), "full": True})
        if classes_json_file:
            plan = json.loads(Path(os.path.expanduser(classes_json_file)).read_text(encoding="utf-8"))
        elif class_schedule_id is not None:
            plan = (await fetch_cj(cli, tri_user_id, int(class_schedule_id)))["cj"] or {}
        else:
            return {"isError": True, "error_code": "BAD_ARGS", "message": "要給 class_schedule_id 或 classes_json_file（才知道工作段）"}
    got = find_key(full, "triUserId")
    if got and got != tri_user_id:
        return {"isError": True, "error_code": "TRI_MISMATCH", "message": f"讀回 triUserId={got}，與要求不符"}
    out = {"activity_id": str(activity_id), "class_schedule_id": class_schedule_id, **ivl.analyze(ivl.series(full), plan, work_pct)}
    if save_to:
        out["saved_to"] = _dump(save_to, out)
    return out


# ---------------------------------------------------------------------------
# 註冊
# ---------------------------------------------------------------------------
LO_STH_EXT_TOOLS = ("lo_sth_prep_week", "lo_sth_update_verified", "lo_sth_diff_week", "lo_sth_verify_intervals")


def register_lo_sth_ext(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tri = {"type": "string", "description": "速查卡〈身分〉的 triUserId（PLAT-27）"}
    tools.append(Tool(name="lo_sth_prep_week", description=(
        "StrongTri 排課前置一次做完（10/02 提速 A）：身分→前 N 天課表＋活動（裝置／手動去重）＋14 天健康＋賽事→"
        "質量課逐堂峰值（功率／配速窗）＋逐段驗收（每趟平均功率／配速、心率、前後半；跑步附每公里）→ 上週每堂 classesJson 存成範本檔＋快照。"
        "摘要附 red_flags（沒排的大量訓練、漏掉的長課）。triUserId 讀回不符即停。完整內容在 save_dir/prep.json。"),
        input_schema={"type": "object", "properties": {
            "tri_user_id": tri, "week_start": {"type": "string", "description": "要排的那週週一 YYYY-MM-DD"},
            "save_dir": {"type": "string", "description": "Mac 上的資料夾（例：…/TP 訓練助理/tmp/<slug>-<週一>）"},
            "history_days": {"type": "integer", "default": 14}, "template_days": {"type": "integer", "default": 7},
            "intervals": {"type": "boolean", "default": True, "description": "質量課逐段驗收（每堂多抓一次逐秒時序）"}},
            "required": ["tri_user_id", "week_start", "save_dir"]}))
    tools.append(Tool(name="lo_sth_update_verified", description=(
        "StrongTri 改課一步（10/02 提速 B）：`text`（summary／trainingAdvice／title）走 preview→token→patch→讀回逐字比對、確認結構沒動；"
        "`classes_json_file` 走整份替換：自動找 scheduleRef／athleteRef→dry-run 取版本→寫→classesJsonMatches＋獨立讀回比對結構與文字。"
        "`recalc_load=true` 寫前先算官方負荷（PLAT-10／17），結構改了官方 STL 卻沒變會警告並給建議值（load_fallback=\"scale\" 直接採用）。"
        "整份替換時 durationSeconds／duration／timeline 一律由各段回填，驗收改看週課表顯示的時長與 STL（week_view）。成功後更新快照。兩者擇一。"),
        input_schema={"type": "object", "properties": {
            "tri_user_id": tri, "class_schedule_id": {"type": "integer"},
            "text": {"type": "object", "description": "{summary?, trainingAdvice?, title?}"},
            "classes_json_file": {"type": "string", "description": "Mac 上的完整新 classesJson 檔"},
            "duration_min": {"type": "integer"}, "recalc_load": {"type": "boolean", "default": False},
            "dry_run": {"type": "boolean", "default": False}, "idempotency_key": {"type": "string"},
            "load_fallback": {"type": "string", "enum": ["warn", "scale"], "default": "warn"}},
            "required": ["tri_user_id", "class_schedule_id"]}))
    tools.append(Tool(name="lo_sth_diff_week", description=(
        "StrongTri 教練改課後比對（10/02 提速 D，STH 版 PROC-80）：拿快照對平台現況，列出移動／刪除／改動的課、"
        "只列有變的段、文字欄位直接給 +／- 差異行、stale_text（文字還留著舊的瓦數／配速／時間）、integrity（教練介面改完留下的矛盾："
        "距離段秒數離譜、週課表時長沒跟著變；有 save_to 會寫出修好的 fix_<id>.json）、新加的課附內容、整週各項 STL（帶 band 就比帶）。"
        "accept=true 把現況收成新快照。"),
        input_schema={"type": "object", "properties": {
            "tri_user_id": tri, "date_from": {"type": "string"}, "date_to": {"type": "string"},
            "accept": {"type": "boolean", "default": False}, "save_to": {"type": "string"},
            "band": {"type": "array", "items": {"type": "integer"}},
            "band_sports": {"type": "array", "items": {"type": "string"}}},
            "required": ["tri_user_id", "date_from", "date_to"]}))
    tools.append(Tool(name="lo_sth_verify_intervals", description=(
        "StrongTri 逐段驗收（STH 版 lo_verify_intervals）：STH 活動沒有 lap，改用逐秒時序（full=true）對計畫的工作段"
        "（強度下緣 ≥ work_pct、≥60 秒）重建每一趟：起點、時長、距離、平均功率／配速、平均／最高心率、前後半；"
        "連續多段（漸進節奏跑）依計畫段長切小段；跑步附每公里分段。給 class_schedule_id 就自己抓計畫，或給 classes_json_file。"),
        input_schema={"type": "object", "properties": {
            "tri_user_id": tri, "activity_id": {"type": "string"},
            "class_schedule_id": {"type": "integer"}, "classes_json_file": {"type": "string"},
            "work_pct": {"type": "number", "default": 85}, "save_to": {"type": "string"}},
            "required": ["tri_user_id", "activity_id"]}))

    async def _h_prep(a): return await lo_sth_prep_week(a["tri_user_id"], a["week_start"], a["save_dir"],
                                                        int(a.get("history_days", 14)), int(a.get("template_days", 7)),
                                                        intervals=bool(a.get("intervals", True)))
    async def _h_upd(a): return await lo_sth_update_verified(a["tri_user_id"], a["class_schedule_id"], a.get("text"),
                                                             a.get("classes_json_file"), a.get("duration_min"),
                                                             bool(a.get("recalc_load", False)), bool(a.get("dry_run", False)),
                                                             a.get("idempotency_key"),
                                                             load_fallback=str(a.get("load_fallback", "warn")))
    async def _h_diff(a): return await lo_sth_diff_week(a["tri_user_id"], a["date_from"], a["date_to"],
                                                        bool(a.get("accept", False)), a.get("save_to"),
                                                        band=a.get("band"), band_sports=a.get("band_sports"))
    async def _h_iv(a): return await lo_sth_verify_intervals(a["tri_user_id"], a["activity_id"], a.get("class_schedule_id"),
                                                             a.get("classes_json_file"), float(a.get("work_pct", 85)),
                                                             a.get("save_to"))

    handlers.update({"lo_sth_prep_week": _h_prep, "lo_sth_update_verified": _h_upd, "lo_sth_diff_week": _h_diff,
                     "lo_sth_verify_intervals": _h_iv})
