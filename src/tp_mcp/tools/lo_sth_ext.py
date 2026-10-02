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
# A：排課前置一次做完
# ---------------------------------------------------------------------------
async def lo_sth_prep_week(tri_user_id: str, week_start: str, save_dir: str, history_days: int = 14,
                           template_days: int = 7, client_factory=_default_factory) -> dict[str, Any]:
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
                                 client_factory=_default_factory, sleep=asyncio.sleep) -> dict[str, Any]:
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
            if recalc_load and cj.get("stages"):
                sport = "CYCLE" if cj.get("sportType") in ("CYCLE", "RIDE") else "RUN"
                r = await cli.call("calculate_workout_sth", {
                    "classes_json": json.dumps(distance_to_time(cj), ensure_ascii=False, separators=(",", ":")),
                    "sport_type": sport, "tri_user_id": tri_user_id})
                sth, stl = find_key(r, "sth"), find_key(r, "stl")
                if isinstance(sth, (int, float)) and sth > 0:
                    cj["sth"], cj["stl"] = int(round(sth)), int(round(stl))
            sref, aref = await _refs_for(cli, tri_user_id, cid, date)
            if not sref:
                return {**out, "isError": True, "error_code": "NO_SCHEDULE_REF", "message": f"{date} 找不到 {cid} 的 scheduleRef"}
            dur = int(duration_min if duration_min is not None else round(int(cj.get("durationSeconds") or 0) / 60))
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
            out.update({"ok": bool(matches) and struct_ok and text_ok, "classesJsonMatches": matches,
                        "structure_verified": struct_ok, "text_verified": text_ok, "sth": cj.get("sth"),
                        "stl": cj.get("stl"), "duration_min": dur, "version": after["version"]})
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


async def lo_sth_diff_week(tri_user_id: str, date_from: str, date_to: str, accept: bool = False,
                           save_to: str | None = None, client_factory=_default_factory) -> dict[str, Any]:
    snaps = snapshots_in_range(tri_user_id, date_from, date_to)
    rows, seen = [], set()
    async with client_factory() as cli:
        wk = await cli.call("get_week_schedule", {"tri_user_id": tri_user_id, "date_from": date_from, "date_to": date_to})
        for date, w in iter_week(wk):
            cid = w.get("classScheduleId")
            seen.add(cid)
            snap = load_snapshot(tri_user_id, cid)
            if snap is None:
                rows.append({"id": cid, "date": date, "title": w.get("title"), "status": "no_snapshot"})
                if accept:
                    got = await fetch_cj(cli, tri_user_id, cid)
                    save_snapshot(tri_user_id, cid, got["cj"], date, got["version"], w.get("title"), "diff_accept")
                continue
            ver = w.get("scheduleVersion")
            moved = snap.get("date") and snap["date"] != date
            if ver and ver == snap.get("scheduleVersion") and not moved:
                continue
            got = await fetch_cj(cli, tri_user_id, cid)
            old, new = snap.get("classes_json") or {}, got["cj"] or {}
            changes = {}
            if moved:
                changes["moved"] = [snap["date"], date]
            for k in ("title", "summary", "trainingAdvice"):
                if _norm(old.get(k)) != _norm(new.get(k)):
                    changes[k] = "changed"
            if _stages_sig(old) != _stages_sig(new):
                changes["structure"] = {"old": _stages_sig(old), "new": _stages_sig(new)}
            for k in ("stl", "durationSeconds", "duration"):
                if old.get(k) != new.get(k):
                    changes[k] = [old.get(k), new.get(k)]
            stale = stale_tokens(old, new) if "structure" in changes else []
            if not changes:
                if accept:
                    save_snapshot(tri_user_id, cid, new, date, got["version"], w.get("title"), "diff_accept")
                continue
            rows.append({"id": cid, "date": date, "title": w.get("title"), "status": "changed",
                         "changes": changes, "stale_text": stale,
                         "text_only": "structure" not in changes and not moved})
            if accept:
                save_snapshot(tri_user_id, cid, new, date, got["version"], w.get("title"), "diff_accept")
        for cid, s in snaps.items():
            if cid not in seen:
                rows.append({"id": cid, "date": s.get("date"), "title": s.get("title"), "status": "deleted"})
    out = {"tri_user_id": tri_user_id, "range": [date_from, date_to],
           "changed": [r for r in rows if r["status"] == "changed"],
           "deleted": [r for r in rows if r["status"] == "deleted"],
           "no_snapshot": [r["id"] for r in rows if r["status"] == "no_snapshot"],
           "stale_total": sum(len([x for x in r.get("stale_text", []) if x.get("old_value_still_in_text")]) for r in rows),
           "accepted": accept,
           "next": "stale_text 裡 old_value_still_in_text=true 的就是說明／本體還留著舊值；照 PROC-80b 以圖形為準改文字，用 lo_sth_update_verified(text=…)；改完再 accept=true 收快照"}
    if save_to:
        out["saved_to"] = _dump(save_to, {**out, "rows": rows})
    return out


# ---------------------------------------------------------------------------
# 註冊
# ---------------------------------------------------------------------------
LO_STH_EXT_TOOLS = ("lo_sth_prep_week", "lo_sth_update_verified", "lo_sth_diff_week")


def register_lo_sth_ext(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tri = {"type": "string", "description": "速查卡〈身分〉的 triUserId（PLAT-27）"}
    tools.append(Tool(name="lo_sth_prep_week", description=(
        "StrongTri 排課前置一次做完（10/02 提速 A）：身分→前 N 天課表＋活動（裝置／手動去重）＋14 天健康＋賽事→"
        "質量課逐堂峰值（功率／配速窗）→ 上週每堂 classesJson 存成範本檔＋快照。triUserId 讀回不符即停。"
        "回傳精簡摘要，完整內容在 save_dir/prep.json。"),
        input_schema={"type": "object", "properties": {
            "tri_user_id": tri, "week_start": {"type": "string", "description": "要排的那週週一 YYYY-MM-DD"},
            "save_dir": {"type": "string", "description": "Mac 上的資料夾（例：…/TP 訓練助理/tmp/<slug>-<週一>）"},
            "history_days": {"type": "integer", "default": 14}, "template_days": {"type": "integer", "default": 7}},
            "required": ["tri_user_id", "week_start", "save_dir"]}))
    tools.append(Tool(name="lo_sth_update_verified", description=(
        "StrongTri 改課一步（10/02 提速 B）：`text`（summary／trainingAdvice／title）走 preview→token→patch→讀回逐字比對、確認結構沒動；"
        "`classes_json_file` 走整份替換：自動找 scheduleRef／athleteRef→dry-run 取版本→寫→classesJsonMatches＋獨立讀回比對結構與文字。"
        "`recalc_load=true` 寫前先算官方負荷（PLAT-10／17）。成功後更新快照。兩者擇一。"),
        input_schema={"type": "object", "properties": {
            "tri_user_id": tri, "class_schedule_id": {"type": "integer"},
            "text": {"type": "object", "description": "{summary?, trainingAdvice?, title?}"},
            "classes_json_file": {"type": "string", "description": "Mac 上的完整新 classesJson 檔"},
            "duration_min": {"type": "integer"}, "recalc_load": {"type": "boolean", "default": False},
            "dry_run": {"type": "boolean", "default": False}, "idempotency_key": {"type": "string"}},
            "required": ["tri_user_id", "class_schedule_id"]}))
    tools.append(Tool(name="lo_sth_diff_week", description=(
        "StrongTri 教練改課後比對（10/02 提速 D，STH 版 PROC-80）：拿快照對平台現況，列出移動／刪除／改動的課、"
        "結構差異（段長、%、組數）與 stale_text（說明段或本體還留著舊的瓦數／配速／時間）。accept=true 把現況收成新快照。"),
        input_schema={"type": "object", "properties": {
            "tri_user_id": tri, "date_from": {"type": "string"}, "date_to": {"type": "string"},
            "accept": {"type": "boolean", "default": False}, "save_to": {"type": "string"}},
            "required": ["tri_user_id", "date_from", "date_to"]}))

    async def _h_prep(a): return await lo_sth_prep_week(a["tri_user_id"], a["week_start"], a["save_dir"],
                                                        int(a.get("history_days", 14)), int(a.get("template_days", 7)))
    async def _h_upd(a): return await lo_sth_update_verified(a["tri_user_id"], a["class_schedule_id"], a.get("text"),
                                                             a.get("classes_json_file"), a.get("duration_min"),
                                                             bool(a.get("recalc_load", False)), bool(a.get("dry_run", False)),
                                                             a.get("idempotency_key"))
    async def _h_diff(a): return await lo_sth_diff_week(a["tri_user_id"], a["date_from"], a["date_to"],
                                                        bool(a.get("accept", False)), a.get("save_to"))

    handlers.update({"lo_sth_prep_week": _h_prep, "lo_sth_update_verified": _h_upd, "lo_sth_diff_week": _h_diff})
