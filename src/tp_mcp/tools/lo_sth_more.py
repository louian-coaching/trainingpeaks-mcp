"""FORK (羅教練 2026/11/16): StrongTri 優化七項（教練選單裁示「全做」）。

  ① lo_sth_get_week_for_validate  STH 讀回轉成 validate_week.py 吃的格式（TP 上百條 R 檢查在 STH 終於跑得到）
  ② sth_text_drafts                 lo_sth_diff_week 的 explanation_drafts（教練改課後的文字數字草稿，只出草稿不寫）
  ③ lo_sth_update_threshold_verified 改 FTP／跑步閾值：寫入→讀回驗→未來課文字數字按比例改（apply_text）
                                    ＋列出距離段被平台重算的時間（PLAT-19）
  ④ pending 佇列                    lo_sth_finish_week 回 pending_device_sync 時記下來；lo_sth_prep_week 與
                                    lo_sth_pending 會自動補排（妞栏山／李敏瑜 11/16 掛兩天的實例）
（⑤ 誤報失敗修在 lo_sth_ext.lo_sth_update_verified；⑥ 在專案的 sth_build.py；⑦ 走官方回報工具）

平台規則照 method/8：PLAT-19／21／22／23／27／30／36。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from tp_mcp.tools.lo_sth import _default_factory, _dump, _ensure_coach, blocking_of, find_key, is_ok

CONFIRM_THRESHOLD = "CONFIRM_THRESHOLD_UPDATE"


# ---------------------------------------------------------------------------
# ④ pending 佇列（同日排序卡在裝置同步）
# ---------------------------------------------------------------------------
def pending_root() -> Path:
    env = os.environ.get("LO_STH_PENDING_DIR")
    return Path(env).expanduser() if env else Path.home() / "trainingpeaks-mcp" / "_scratch" / "sth_pending"


def _pending_file(tri: str) -> Path:
    return pending_root() / f"{tri}.json"


def list_pending(tri: str | None = None) -> dict[str, list[dict[str, Any]]]:
    root = pending_root()
    out: dict[str, list[dict[str, Any]]] = {}
    if not root.exists():
        return out
    for f in sorted(root.glob("*.json")):
        if tri and f.stem != tri:
            continue
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if items:
            out[f.stem] = items
    return out


def add_pending(tri: str, payload_file: str, days: list[str]) -> None:
    items = list_pending(tri).get(tri, [])
    items = [i for i in items if i.get("payload_file") != payload_file]
    items.append(
        {
            "payload_file": payload_file,
            "days": sorted(set(days)),
            "since": dt.datetime.now().isoformat(timespec="seconds"),
        }
    )
    f = _pending_file(tri)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def clear_pending(tri: str, payload_file: str) -> None:
    f = _pending_file(tri)
    if not f.exists():
        return
    items = [i for i in list_pending(tri).get(tri, []) if i.get("payload_file") != payload_file]
    if items:
        f.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        try:
            f.unlink()
        except OSError:
            f.write_text("[]", encoding="utf-8")


async def run_pending(
    tri: str | None = None, client_factory=_default_factory, sleep=None, time_budget_s: int = 0
) -> list[dict[str, Any]]:
    """把佇列裡的同日排序再試一次。time_budget_s=0＝撞 409001 不等、留在佇列（prep_week 用，避免逾時）。"""
    import asyncio

    from tp_mcp.tools.lo_sth import lo_sth_reorder_week

    rows = []
    for t, items in list_pending(tri).items():
        for it in items:
            pf = it["payload_file"]
            if not Path(os.path.expanduser(pf)).exists():
                clear_pending(t, pf)
                rows.append({"tri_user_id": t, "payload_file": pf, "status": "dropped", "note": "payload 檔不在了"})
                continue
            try:
                ro = await lo_sth_reorder_week(
                    pf,
                    retry_wait_s=15,
                    retries=99,
                    client_factory=client_factory,
                    sleep=sleep or asyncio.sleep,
                    include_existing=True,
                    time_budget_s=time_budget_s,
                )
            except Exception as e:  # noqa: BLE001 — 補排失敗不擋呼叫端
                rows.append({"tri_user_id": t, "payload_file": pf, "status": "error", "error": str(e)[:160]})
                continue
            still = [r["date"] for r in ro.get("rows", []) if r.get("status") == "pending_sync"]
            bad = [r["date"] for r in ro.get("rows", []) if r.get("status") == "failed"]
            if not still and not bad:
                clear_pending(t, pf)
                rows.append(
                    {
                        "tri_user_id": t,
                        "payload_file": pf,
                        "status": "done",
                        "days": [r["date"] for r in ro.get("rows", [])],
                        "since": it.get("since"),
                    }
                )
            else:
                rows.append(
                    {
                        "tri_user_id": t,
                        "payload_file": pf,
                        "status": "still_pending" if still else "failed",
                        "pending_days": still,
                        "failed_days": bad,
                        "since": it.get("since"),
                    }
                )
    return rows


async def lo_sth_pending(
    tri_user_id: str | None = None, run: bool = True, time_budget_s: int = 30, client_factory=_default_factory
) -> dict[str, Any]:
    q = list_pending(tri_user_id)
    if not run:
        return {"pending": q, "count": sum(len(v) for v in q.values())}
    rows = await run_pending(tri_user_id, client_factory=client_factory, time_budget_s=time_budget_s)
    left = list_pending(tri_user_id)
    return {
        "rows": rows,
        "remaining": left,
        "remaining_count": sum(len(v) for v in left.values()),
        **({"next": "仍有日子卡在裝置同步：過幾分鐘再叫一次 lo_sth_pending"} if left else {}),
    }


# ---------------------------------------------------------------------------
# ② 教練改課後的文字草稿
# ---------------------------------------------------------------------------
def _n(s: Any) -> str:
    return "\n".join(x.rstrip() for x in str(s or "").strip().splitlines())


def sth_text_drafts(
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
    stale: list[dict[str, Any]] | None,
    date_old: str | None = None,
    date_new: str | None = None,
) -> dict[str, Any] | None:
    """summary／trainingAdvice 的整份草稿：①教練改了 summary → 依 summary 前後的數字對應改 trainingAdvice
    ②教練只改圖形 → 依各段新舊值（stale_text）改 summary 與 trainingAdvice ③移日期 → 「比賽後第 N 天」「D±N」推算。
    瓦數／配速區間（有 ~）直接換；單一時間／距離在同一欄位出現兩次以上＝位置判不準，列 unresolved。只出草稿、不寫入。"""
    from tp_mcp.tools import lo_expl_sync as ex

    old, new = old or {}, new or {}
    coach_summary = _n(old.get("summary")) != _n(new.get("summary"))
    pairs: dict[str, set[str]] = {}
    if coach_summary:
        for o, nv in ex.number_substitutions(_n(old.get("summary")), _n(new.get("summary"))):
            pairs.setdefault(o, set()).add(nv)
    removed: list[str] = []
    for x in stale or []:
        if x.get("kind") not in ("power", "pace", "dur", "dist") or not x.get("old"):
            continue
        if x.get("new"):
            pairs.setdefault(ex._norm(x["old"]), set()).add(ex._norm(x["new"]))
        elif x.get("old_value_still_in_text"):
            removed.append(x["old"])
    ambiguous = sorted(o for o, ns in pairs.items() if len(ns) > 1)
    fields = ["trainingAdvice"] + ([] if coach_summary else ["summary"])
    out: dict[str, Any] = {}
    for f in fields:
        text = str(new.get(f) or "")
        orig = text
        reps: list[dict[str, str]] = []
        unresolved: list[str] = []
        for o, ns in sorted(pairs.items()):
            if o in ambiguous:
                continue
            nv = next(iter(ns))
            rx = ex._token_regex(o)
            hits = list(rx.finditer(text))
            if not hits:
                continue
            if "~" not in o and len(hits) > 1:
                unresolved.append(o)
                continue
            for m in hits:
                reps.append({"from": m.group(0), "to": ex._swap(m.group(0), nv), "context": ex._ctx(text, m)})
            text = rx.sub(lambda m, nv=nv: ex._swap(m.group(0), nv), text)
        check: list[str] = []
        if date_old and date_new and date_old[:10] != date_new[:10]:
            r = ex.draft_explanation("", "", text, date_old[:10], date_new[:10])
            text, check = r["draft"], r["check_phrases"]
            reps += r["replacements"]
        unresolved += [o for o in ambiguous if ex._token_regex(o).search(text)]
        unresolved += [o for o in removed if ex._token_regex(ex._norm(o)).search(text)]
        if text != orig or unresolved or check:
            out[f] = {
                "draft": text,
                "changed": text != orig,
                "replacements": reps,
                "unresolved": sorted(set(unresolved)),
                "check_phrases": check,
            }
    if not out:
        return None
    patch = {f: d["draft"] for f, d in out.items() if d["changed"]}
    return {"fields": out, "text_patch": patch, "coach_edited_summary": coach_summary}


# ---------------------------------------------------------------------------
# ① STH 週課表 → validate_week.py 格式
# ---------------------------------------------------------------------------
_SPORT = {"CYCLE": "Bike", "RIDE": "Bike", "RUN": "Run", "SWIM": "Swim", "STRENGTH": "Strength", "REST": "DayOff"}
_IC = {"warmup": "warmUp", "cooling": "coolDown", "cooldown": "coolDown", "recover": "rest", "rest": "rest"}


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def stages_to_structure(cj: dict[str, Any]) -> dict[str, Any] | None:
    stages = cj.get("stages") or []
    if not stages:
        return None
    bike = str(cj.get("sportType") or "").upper() in ("CYCLE", "RIDE")
    blocks, t, any_dist = [], 0, False
    for st in stages:
        times = int(_num(st.get("times")) or 1)
        steps, sec = [], 0
        for s in st.get("sections") or []:
            if s.get("capacity") == "distance":
                any_dist = True
                length = {"value": int(round(_num(s.get("targetDistance")) * 1000)), "unit": "meter"}
            else:
                length = {"value": int(_num(s.get("targetSeconds"))), "unit": "second"}
            sec += int(_num(s.get("targetSeconds")))
            rng = s.get("thresholdFtpRange") if bike else s.get("thresholdSpeedRange")
            if not (isinstance(rng, list) and len(rng) == 2):
                one = s.get("thresholdFtp") if bike else s.get("thresholdSpeed")
                rng = [one, one] if one is not None else [0, 0]
            targets = [{"minValue": _num(rng[0]), "maxValue": _num(rng[1])}]
            cad = s.get("cadence")
            if s.get("hasCadence") and isinstance(cad, list) and len(cad) == 2:
                targets.append({"minValue": _num(cad[0]), "maxValue": _num(cad[1]), "unit": "roundOrStridePerMinute"})
            # section title（热身／骑行／恢复）是平台上色用的分類標籤、不是我們寫給選手的字 → 不當 step name，
            # 免得 R51 繁體檢查對平台標籤誤報
            steps.append(
                {
                    "name": "",
                    "_sth_title": s.get("title"),
                    "type": "step",
                    "length": length,
                    "targets": targets,
                    "intensityClass": _IC.get(str(s.get("stageMode") or ""), "active"),
                    "openDuration": False,
                }
            )
        blocks.append(
            {
                "type": "repetition" if times > 1 else "step",
                "length": {"value": times, "unit": "repetition"},
                "steps": steps,
                "begin": t,
                "end": t + sec * times,
            }
        )
        t += sec * times
    return {
        "structure": blocks,
        "primaryLengthMetric": "distance" if any_dist else "duration",
        "primaryIntensityMetric": "percentOfFtp" if bike else "percentOfThresholdPace",
        "primaryIntensityTargetOrRange": "range",
        "_source": "strongtri_stages",
    }


def to_validate_workout(date: str, w: dict[str, Any], cj: dict[str, Any] | None) -> dict[str, Any]:
    cj = cj or {}
    sp = str(w.get("sportType") or cj.get("sportType") or "").upper()
    summary, advice = _n(cj.get("summary")), _n(cj.get("trainingAdvice"))
    desc = summary + (("\n－－\n" + advice) if advice else "")
    links = [x.get("url") for x in (cj.get("links") or []) if isinstance(x, dict) and x.get("url")]
    if links:  # STH 的連結走 links 欄位（PLAT-11）；接回說明段尾，R5b 等「附不附連結」的規則才驗得到
        desc += "\n" + "\n".join(links)
    dur_s = _num(w.get("plannedDurationSec")) or _num(w.get("durationMin")) * 60
    dist_m = _num(w.get("plannedDistanceMeters"))
    out = {
        "id": str(w.get("classScheduleId")),
        "date": date[:10],
        "title": w.get("title") or cj.get("title"),
        "sport": _SPORT.get(sp, "Other"),
        "type": "planned",
        "description": desc,
        "duration_planned": round(dur_s / 3600, 3) if dur_s else None,
        "tss_planned": _num(w.get("plannedSTL")) or _num(cj.get("stl")) or None,
        "distance_planned_km": round(dist_m / 1000, 3) if dist_m else None,
        "_sth": {"bound": bool((w.get("activityBinding") or {}).get("bound")), "links": links},
    }
    sw = stages_to_structure(cj)
    if sw:
        out["structured_workout"] = sw
    return out


async def lo_sth_get_week_for_validate(
    tri_user_id: str, date_from: str, date_to: str, save_to: str, client_factory=_default_factory
) -> dict[str, Any]:
    from tp_mcp.tools.lo_sth_ext import fetch_cj, iter_week

    async with client_factory() as cli:
        ident = await _ensure_coach(cli)
        wk = await cli.call(
            "get_week_schedule", {"tri_user_id": tri_user_id, "date_from": date_from, "date_to": date_to}
        )
        got = find_key(wk, "triUserId")
        if got and got != tri_user_id:
            return {"isError": True, "error_code": "TRI_MISMATCH", "message": f"讀回 triUserId={got}，與要求不符，停止"}
        rows, errors = [], []
        for d, w in iter_week(wk):
            if str(w.get("sportType") or "").upper() == "REST":
                rows.append(to_validate_workout(d, w, {}))
                continue
            try:
                cj = (await fetch_cj(cli, tri_user_id, w.get("classScheduleId")))["cj"]
            except Exception as e:  # noqa: BLE001
                errors.append({"id": w.get("classScheduleId"), "error": str(e)[:120]})
                cj = None
            rows.append(to_validate_workout(d, w, cj))
    strength_days = sorted({r["date"] for r in rows if r["sport"] == "Strength" and "活動度" not in (r["title"] or "")})
    data = {"platform": "strongtri", "tri_user_id": tri_user_id, "date_range": [date_from, date_to], "workouts": rows}
    path = _dump(save_to, data)
    flags = ["--country China"] + ([f"--strength-days {','.join(strength_days)}"] if strength_days else [])
    return {
        "identity": ident,
        "saved_to": path,
        "count": len(rows),
        "by_sport": {s: sum(r["sport"] == s for r in rows) for s in sorted({r["sport"] for r in rows})},
        "suggested_flags": " ".join(flags),
        **({"fetch_errors": errors} if errors else {}),
        "next": (
            "python3 tp-ai-layer/tools/validate_week.py <saved_to> "
            + " ".join(flags)
            + " ＋速查卡平常帶的旗標（--variants／--prev／--run-threshold…）。檔頭 platform=strongtri："
            "R3／R4 polyline 自動跳過（STH 沒有 polyline）；其餘 R 規則照驗"
        ),
    }


# ---------------------------------------------------------------------------
# ③ 改閾值（FTP／跑步配速）＋文字同步
# ---------------------------------------------------------------------------
_KINDS = {"ftp": (2, "power", ("CYCLE", "RIDE"), "ftp_bike"), "run_pace": (3, "run_pace", ("RUN",), "run_pace")}


def _pace_to_sec(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if ":" in s:
        m, sec = s.split(":", 1)
        return int(m) * 60 + float(sec)
    return float(s)


def _thr_value(thr: dict[str, Any], key: str) -> float | None:
    t = (thr.get("thresholds") or {}).get(key) or {}
    v = t.get("threshold")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rewrite(tp_kind: str, old: float, new: float, text: str) -> tuple[str, int]:
    from tp_mcp.tools.lo_threshold import rewrite_text

    if tp_kind == "run_pace":  # lo_threshold 吃速度（m/s）；STH 閾值是秒/公里 → 換成速度再丟
        return rewrite_text(tp_kind, 1000.0 / old, 1000.0 / new, text)
    return rewrite_text(tp_kind, old, new, text)


def _dist_secs(cj: dict[str, Any] | None) -> list[tuple[int, int, float, int]]:
    out = []
    for si, st in enumerate((cj or {}).get("stages") or []):
        for ci, s in enumerate(st.get("sections") or []):
            if s.get("capacity") == "distance":
                out.append((si, ci, _num(s.get("targetDistance")), int(_num(s.get("targetSeconds")))))
    return out


async def lo_sth_update_threshold_verified(
    tri_user_id: str,
    kind: str,
    value: Any,
    dry_run: bool = False,
    apply_text: str = "preview",
    scan_days: int = 42,
    today: str | None = None,
    client_factory=_default_factory,
) -> dict[str, Any]:
    from tp_mcp.tools.lo_sth_ext import fetch_cj, iter_week, lo_sth_update_verified, load_snapshot, save_snapshot

    if kind not in _KINDS:
        return {
            "isError": True,
            "error_code": "BAD_KIND",
            "message": "kind 只收 ftp／run_pace（心率閾值是騎跑共用單值 PLAT-22，平台層不改；游泳 CSS 不在本工具）",
        }
    if apply_text not in ("none", "preview", "write"):
        return {"isError": True, "error_code": "BAD_ARGS", "message": "apply_text 只收 none／preview／write"}
    ttype, key, sports, tp_kind = _KINDS[kind]
    new = float(value) if kind == "ftp" else _pace_to_sec(value)
    d0 = dt.date.fromisoformat(today) if today else dt.date.today()
    d1 = d0 + dt.timedelta(days=int(scan_days))
    out: dict[str, Any] = {
        "tri_user_id": tri_user_id,
        "kind": kind,
        "threshold_type": ttype,
        "new": new,
        "mode": "dry_run" if dry_run else "write",
        "scan": [str(d0), str(d1)],
    }
    async with client_factory() as cli:
        out["identity"] = await _ensure_coach(cli)
        if out["identity"]["identityType"] != "C":
            return {**out, "isError": True, "error_code": "IDENTITY_NOT_COACH"}
        thr0 = await cli.call("get_athlete_thresholds", {"tri_user_id": tri_user_id})
        got = find_key(thr0, "triUserId")
        if got and got != tri_user_id:
            return {**out, "isError": True, "error_code": "TRI_MISMATCH", "message": f"讀回 triUserId={got}，停止"}
        old = _thr_value(thr0, key)
        out["old"] = old
        if not old:
            return {**out, "isError": True, "error_code": "NO_OLD_THRESHOLD", "message": f"平台上讀不到 {key} 的現值"}
        if abs(old - new) < 0.5:
            return {**out, "ok": True, "status": "no_change", "message": "平台現值已是這個數字，沒有寫入"}
        # 未來課（不含已綁活動＝已執行的課）
        before: dict[Any, dict[str, Any]] = {}
        aref = None
        d = d0
        while d <= d1:
            e = min(d + dt.timedelta(days=6), d1)
            wk = await cli.call(
                "get_week_schedule", {"tri_user_id": tri_user_id, "date_from": str(d), "date_to": str(e)}
            )
            aref = aref or wk.get("athleteRef")
            for day, w in iter_week(wk):
                aref = aref or w.get("athleteRef")
                if str(w.get("sportType") or "").upper() not in sports:
                    continue
                if (w.get("activityBinding") or {}).get("bound"):
                    continue
                got_cj = await fetch_cj(cli, tri_user_id, w.get("classScheduleId"))
                before[w.get("classScheduleId")] = {
                    "date": day,
                    "title": w.get("title"),
                    "cj": got_cj["cj"] or {},
                    "plannedDurationSec": w.get("plannedDurationSec"),
                }
            d = e + dt.timedelta(days=1)
        if not aref:
            return {
                **out,
                "isError": True,
                "error_code": "NO_ATHLETE_REF",
                "message": "掃描區間沒有課、拿不到 athleteRef："
                "先 prepare_schedule_assignment(athlete_query=暱稱) 取 ref 手動寫",
            }
        args = {"athlete_ref": aref, "threshold_type": ttype, "threshold": int(round(new))}
        dry = await cli.call("apply_threshold_update", {**args, "dry_run": True})
        out["impact"] = {
            k: find_key(dry, k)
            for k in ("deviceSyncImpact", "affectedScheduleCount", "proposedZones")
            if find_key(dry, k) is not None
        }
        if blocking_of(dry) or dry.get("isError"):
            return {
                **out,
                "isError": True,
                "error_code": "DRY_RUN_BLOCKED",
                "blocking": blocking_of(dry)[:5],
                "raw_head": json.dumps(dry, ensure_ascii=False)[:300],
            }
        after_cj: dict[Any, dict[str, Any]] = {}
        if not dry_run:
            res = await cli.call(
                "apply_threshold_update",
                {**args, "dry_run": False, "confirm_write": True, "confirm_token": CONFIRM_THRESHOLD},
            )
            thr1 = await cli.call("get_athlete_thresholds", {"tri_user_id": tri_user_id})
            landed = _thr_value(thr1, key)
            collateral = [
                k
                for k in ("heart_rate", "power", "run_pace", "swim_pace")
                if k != key and _thr_value(thr0, k) != _thr_value(thr1, k)
            ]
            out["landed"] = landed
            out["zones_after"] = ((thr1.get("thresholds") or {}).get(key) or {}).get("zones")
            if landed is None or abs(landed - new) >= 0.5:
                return {
                    **out,
                    "isError": True,
                    "error_code": "WRITE_NOT_LANDED",
                    "platform_ok": is_ok(res),
                    "raw_head": json.dumps(res, ensure_ascii=False)[:300],
                    "message": "讀回不是新值＝沒寫進去（平台旗標不算數，PLAT-23）；文字一個都沒改",
                }
            if collateral:
                out["warnings"] = [{"code": "COLLATERAL_CHANGE", "types": collateral}]
            if not is_ok(res):
                out.setdefault("warnings", []).append(
                    {"code": "PLATFORM_FLAG_FALSE_NEGATIVE", "message": "平台旗標回失敗，但讀回已是新值（PLAT-23）"}
                )
            # PLAT-19：距離段的 targetSeconds 會被平台重算
            for cid, b in before.items():
                got_cj = await fetch_cj(cli, tri_user_id, cid)
                after_cj[cid] = got_cj["cj"] or {}
                if load_snapshot(tri_user_id, cid) is not None:  # 平台重算不是教練改課，快照跟上
                    save_snapshot(
                        tri_user_id, cid, after_cj[cid], b["date"], got_cj["version"], b["title"], "threshold_update"
                    )
    # 文字數字（PLAT-21：平台不動 summary／trainingAdvice）
    text_rows, retimed = [], []
    for cid, b in before.items():
        cj = after_cj.get(cid) or b["cj"]
        patch, hits = {}, 0
        for f in ("summary", "trainingAdvice"):
            t = str(cj.get(f) or "")
            nt, h = _rewrite(tp_kind, old, new, t)
            if h and nt != t:
                patch[f] = nt
                hits += h
        if patch:
            text_rows.append({"id": cid, "date": b["date"], "title": b["title"], "hits": hits, "patch": patch})
        if cid in after_cj:
            a_d = {(si, ci): s for si, ci, _, s in _dist_secs(after_cj[cid])}
            for si, ci, km, s0 in _dist_secs(b["cj"]):
                s1 = a_d.get((si, ci))
                if s1 is not None and s1 != s0:
                    retimed.append(
                        {
                            "id": cid,
                            "date": b["date"],
                            "title": b["title"],
                            "stage": si,
                            "section": ci,
                            "km": km,
                            "targetSeconds": [s0, s1],
                        }
                    )
    out["future_sessions"] = len(before)
    out["text"] = {
        "mode": apply_text,
        "sessions": [{k: r[k] for k in ("id", "date", "title", "hits")} for r in text_rows],
    }
    if apply_text == "preview" or (apply_text == "write" and dry_run):
        out["text"]["preview"] = text_rows
    elif apply_text == "write":
        done = []
        for r in text_rows:
            u = await lo_sth_update_verified(tri_user_id, r["id"], text=r["patch"], client_factory=client_factory)
            done.append(
                {
                    "id": r["id"],
                    "date": r["date"],
                    "ok": bool(u.get("ok")),
                    **({"error_code": u.get("error_code")} if not u.get("ok") else {}),
                }
            )
        out["text"]["written"] = done
        if any(not x["ok"] for x in done):
            out.setdefault("warnings", []).append(
                {"code": "TEXT_WRITE_INCOMPLETE", "ids": [x["id"] for x in done if not x["ok"]]}
            )
    if retimed:
        out["distance_retimed"] = retimed
        out.setdefault("warnings", []).append(
            {
                "code": "PLAT19_DISTANCE_RETIMED",
                "message": "距離段的 targetSeconds 被平台依新閾值重算（PLAT-19）。要維持新閾值的配速＝不用動；"
                "要維持原本的絕對配速＝整份替換把 % 改回、targetSeconds 寫回原值（教練決定）",
            }
        )
    out["ok"] = not out.get("isError")
    if dry_run:
        out["next"] = "確認 impact 與 text.preview 後，同樣參數 dry_run=false 真寫（apply_text=write 會連文字一起改）"
    return out


# ---------------------------------------------------------------------------
# 註冊
# ---------------------------------------------------------------------------
LO_STH_MORE_TOOLS = ("lo_sth_get_week_for_validate", "lo_sth_update_threshold_verified", "lo_sth_pending")


def register_lo_sth_more(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(
        Tool(
            name="lo_sth_get_week_for_validate",
            description=(
                "STH 週課表轉 validate_week.py 格式存到 save_to（Mac 路徑）："
                "description＝summary＋－－＋trainingAdvice＋links；"
                "stages→structured_workout；tss_planned＝plannedSTL。檔頭 platform=strongtri（R3/R4 自動跳過）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tri_user_id": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "save_to": {"type": "string", "description": "Mac 上的輸出路徑（JSON）"},
                },
                "required": ["tri_user_id", "date_from", "date_to", "save_to"],
            },
        )
    )
    tools.append(
        Tool(
            name="lo_sth_update_threshold_verified",
            description=(
                "STH 改 FTP（kind=ftp）或跑步閾值（kind=run_pace，'5:04' 或秒）："
                "dry-run→寫入→讀回驗新值與其他閾值沒動；"
                "列出被重算的距離段秒數（PLAT-19）；"
                "apply_text=preview 回未來課文字按比例改寫草稿、write 逐堂寫入並讀回（PLAT-21）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tri_user_id": {"type": "string"},
                    "kind": {"type": "string", "enum": ["ftp", "run_pace"]},
                    "value": {"type": ["number", "string"]},
                    "dry_run": {"type": "boolean", "default": False},
                    "apply_text": {"type": "string", "enum": ["none", "preview", "write"], "default": "preview"},
                    "scan_days": {"type": "integer", "default": 42},
                    "today": {"type": "string", "description": "YYYY-MM-DD，掃描起點（預設今天）"},
                },
                "required": ["tri_user_id", "kind", "value"],
            },
        )
    )
    tools.append(
        Tool(
            name="lo_sth_pending",
            description=(
                "STH 同日排序待辦：finish_week 回 pending_device_sync 時自動記下；"
                "run=true 再排一次、排好移出（不帶 tri_user_id＝全部）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tri_user_id": {"type": "string"},
                    "run": {"type": "boolean", "default": True},
                    "time_budget_s": {"type": "integer", "default": 30},
                },
            },
        )
    )

    async def _h_val(a):
        return await lo_sth_get_week_for_validate(a["tri_user_id"], a["date_from"], a["date_to"], a["save_to"])

    async def _h_thr(a):
        return await lo_sth_update_threshold_verified(
            a["tri_user_id"],
            a["kind"],
            a["value"],
            dry_run=bool(a.get("dry_run", False)),
            apply_text=str(a.get("apply_text", "preview")),
            scan_days=int(a.get("scan_days", 42)),
            today=a.get("today"),
        )

    async def _h_pend(a):
        return await lo_sth_pending(a.get("tri_user_id"), bool(a.get("run", True)), int(a.get("time_budget_s", 30)))

    handlers.update(
        {"lo_sth_get_week_for_validate": _h_val, "lo_sth_update_threshold_verified": _h_thr, "lo_sth_pending": _h_pend}
    )
