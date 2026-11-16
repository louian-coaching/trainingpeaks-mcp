"""FORK (羅教練 2026/11/16): 選手設定的讀查與寫入（TP MCP 優化，教練選單「四項都做」）。

  lo_settings_audit   全名單（或指定名單）設定健檢，只列不改：心率錨點異常（靜息 185 之類）、
                      通用軌與專項閾值不一致、缺專項區間、池長停在預設 25 碼、帳號 basic。
  lo_set_pool_length  設預設池長（TP 網頁同一組 API：poollengthsettings/option ＋ defaultoption），
                      先找現成選項、沒有才新增，寫完讀回驗 defaultId。

背景：2026/10/19「TP 網頁手改清單」把池長、靜息心率、缺騎車心率區間列為只能教練網頁改；
11/16 查到上游工具其實寫得到（心率錨點／建區間），池長端點從 TP 網頁程式碼確認：
  GET  fitness/v1/athletes/{id}/poollengthsettings
  POST fitness/v1/athletes/{id}/poollengthsettings/option        {id:null,length,units,label,status}
  PUT  fitness/v1/athletes/{id}/poollengthsettings/defaultoption/{optionId}   （body null）
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from tp_mcp.client import TPClient
from tp_mcp.client.context import athlete_override

logger = logging.getLogger(__name__)

_ARRAYS = ("powerZones", "heartRateZones", "speedZones")


def _wt_names() -> dict[int, str]:
    from tp_mcp.tools.settings import _ZONE_WTID
    out: dict[int, str] = {}
    for name, wid in _ZONE_WTID.items():
        if wid not in out or name in ("general", "bike", "run", "swim"):
            out[wid] = name
    return out


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def audit_settings(settings: dict[str, Any], pool: dict[str, Any] | None = None,
                   account: dict[str, Any] | None = None, tol: float = 0.02) -> dict[str, Any]:
    """Pure: one athlete's settings → {sets, flags}. Flags are things a coach would want to fix."""
    names = _wt_names()
    flags: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []
    sets: dict[str, list[str]] = {}
    for arr in _ARRAYS:
        groups = [g for g in (settings.get(arr) or []) if isinstance(g, dict)]
        sets[arr] = [names.get(g.get("workoutTypeId"), str(g.get("workoutTypeId"))) for g in groups]
        general = next((g for g in groups if g.get("workoutTypeId") == 0), None)
        for g in groups:
            wt = names.get(g.get("workoutTypeId"), str(g.get("workoutTypeId")))
            thr = _num(g.get("threshold"))
            if not thr or thr <= 0:
                flags.append({"code": "THRESHOLD_MISSING", "array": arr, "set": wt})
            if arr == "heartRateZones":
                rest, mx = _num(g.get("restingHeartRate")), _num(g.get("maximumHeartRate"))
                if rest and mx and rest >= mx:
                    flags.append({"code": "HR_RESTING_GE_MAX", "set": wt, "resting": rest, "max": mx})
                elif rest and rest > 100:
                    flags.append({"code": "HR_RESTING_IMPLAUSIBLE", "set": wt, "resting": rest})
                if mx and mx < 130:
                    flags.append({"code": "HR_MAX_IMPLAUSIBLE", "set": wt, "max": mx})
                if thr and mx and thr > mx:
                    flags.append({"code": "HR_LTHR_ABOVE_MAX", "set": wt, "lthr": thr, "max": mx})
            # 通用軌 vs 專項：只記成 info（11/16 全名單實跑 328 筆、多半無害）；游泳速度跟通用軌本來就不同單位尺度，不比
            if general is not None and g is not general and thr and not (arr == "speedZones" and wt == "swim"):
                gthr = _num(general.get("threshold"))
                if gthr and abs(gthr - thr) / thr > tol:
                    info.append({"code": "GENERAL_DIFFERS", "array": arr, "set": wt,
                                 "general": general.get("threshold"), "sport": g.get("threshold")})
    if pool:
        did = pool.get("defaultId")
        opt = next((o for o in pool.get("options") or [] if o.get("id") == did), None)
        if opt and str(opt.get("units")).lower().startswith("yard"):
            flags.append({"code": "POOL_DEFAULT_YARDS", "defaultId": did, "length": opt.get("length"),
                          "note": "游泳距離會被當碼算（25 碼＝22.86 公尺，低估 8.6%）"})
    if account and account.get("tier") == "basic":
        flags.append({"code": "ACCOUNT_BASIC", "tier": "basic"})
    return {"sets": sets, "flags": flags, "info": info}


async def _read_one(requested: str) -> dict[str, Any]:
    from tp_mcp.tools.lo_review import athlete_identity
    from tp_mcp.tools.settings import tp_get_athlete_settings, tp_get_pool_length_settings
    token = athlete_override.set(requested)
    try:
        ident = await athlete_identity()
        s = await tp_get_athlete_settings()
        p = await tp_get_pool_length_settings()
        if s.get("isError"):
            return {"athlete": requested, **ident, "error": f"{s.get('error_code')} {s.get('message')}"}
        return {"athlete": requested, **ident, "settings": s.get("settings") or {},
                "pool": (p or {}).get("pool_length_settings") if not (p or {}).get("isError") else None}
    finally:
        athlete_override.reset(token)


async def lo_settings_audit(athletes: list[str] | None = None, save_to: str | None = None,
                            concurrency: int = 3, include_self: bool = False) -> dict[str, Any]:
    from tp_mcp.tools.profile import tp_list_athletes
    roster = await tp_list_athletes()
    acc = {str(a.get("athlete_id")): a for a in (roster or {}).get("athletes") or []}
    if athletes:
        targets = [str(a).strip() for a in athletes if str(a).strip()]
    else:
        targets = [aid for aid, a in acc.items() if include_self or not a.get("is_self")]
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(t: str) -> dict[str, Any]:
        async with sem:
            try:
                r = await _read_one(t)
            except Exception as e:  # noqa: BLE001 — one athlete never sinks the audit
                return {"athlete": t, "error": str(e)[:160]}
            if r.get("error"):
                return r
            a = acc.get(str(r.get("athlete_id"))) or {}
            res = audit_settings(r["settings"], r.get("pool"), a.get("account"))
            return {"athlete_id": r.get("athlete_id"), "name": r.get("athlete_name") or a.get("name"),
                    **res, "_raw": {"settings": {k: r["settings"].get(k) for k in _ARRAYS}, "pool": r.get("pool")}}

    rows = await asyncio.gather(*(one(t) for t in targets))
    flagged = [{k: v for k, v in r.items() if k != "_raw"} for r in rows if r.get("flags")]
    counts: dict[str, int] = {}
    for r in rows:
        for f in r.get("flags") or []:
            counts[f["code"]] = counts.get(f["code"], 0) + 1
    out: dict[str, Any] = {"scanned": len(rows), "flagged": len(flagged), "by_code": counts,
                           "rows": flagged, "errors": [r for r in rows if r.get("error")],
                           "note": ("只列不改；flags＝該修的、rows[].info＝通用軌與專項不同（多半無害）。"
                                    "靜息＝最大（185／185 這類）在 Friel LTHR 法（method 1／2／3）"
                                    "不影響區間，只是錨點錯值。"
                                    "修心率錨點／建區間＝lo_update_threshold_verified"
                                    "（kind=lthr、resting_hr／max_hr、create_if_missing）；"
                                    "池長＝lo_set_pool_length；改數值前先問教練（權限邊界）。"
                                    "GENERAL_DIFFERS＝通用軌（workoutTypeId 0）與專項不同，"
                                    "只有課會落到通用軌時才有影響。")}
    if save_to:
        p = Path(save_to).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({**out, "rows_all": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        out["saved_to"] = str(p)
    return out


async def lo_set_pool_length(length: float, units: str = "Meters", dry_run: bool = False) -> dict[str, Any]:
    from tp_mcp.tools.lo_review import athlete_identity
    u = {"m": "Meters", "meters": "Meters", "meter": "Meters", "y": "Yards", "yards": "Yards", "yard": "Yards"}.get(
        str(units).strip().lower())
    if not u:
        return {"isError": True, "error_code": "INVALID_ARGS", "message": "units must be Meters or Yards"}
    try:
        length = float(length)
    except (TypeError, ValueError):
        return {"isError": True, "error_code": "INVALID_ARGS", "message": "length must be a number"}
    if not 10 <= length <= 100:
        return {"isError": True, "error_code": "INVALID_ARGS", "message": "length out of range (10–100)"}
    ident = await athlete_identity()
    async with TPClient() as client:
        aid = await client.ensure_athlete_id()
        if not aid:
            return {"isError": True, "error_code": "AUTH_INVALID", "message": "Could not get athlete ID."}
        base = f"/fitness/v1/athletes/{aid}/poollengthsettings"
        r0 = await client.get(base)
        if r0.is_error or not isinstance(r0.data, dict):
            return {"isError": True, "error_code": "API_ERROR", "message": r0.message or "no pool settings"}
        before = r0.data
        opts = before.get("options") or []
        match = [o for o in opts if abs(float(o.get("length") or 0) - length) < 0.01 and o.get("units") == u]
        match.sort(key=lambda o: not str(o.get("id", "")).startswith("DEFAULT-"))
        cur = next((o for o in opts if o.get("id") == before.get("defaultId")), None)
        out: dict[str, Any] = {**ident, "before": {"defaultId": before.get("defaultId"), "default": cur},
                               "target": {"length": length, "units": u}, "dry_run": dry_run}
        if match and match[0].get("id") == before.get("defaultId"):
            return {**out, "success": True, "status": "no_change"}
        out["plan"] = (f"set default → existing option {match[0]['id']}" if match
                       else "create option, then set it as default")
        if dry_run:
            return {**out, "success": True}
        if match:
            opt_id = match[0]["id"]
        else:
            rc = await client.post(f"{base}/option", json={"id": None, "length": length, "units": u,
                                                           "label": None, "status": None})
            if rc.is_error:
                return {**out, "isError": True, "error_code": "API_ERROR", "message": f"create option: {rc.message}"}
            opt_id = rc.data if isinstance(rc.data, (str, int)) else (rc.data or {}).get("id")
            if not opt_id:  # 回應沒帶 id → 重讀找剛建的那筆
                rr = await client.get(base)
                new = [o for o in ((rr.data or {}).get("options") or [])
                       if abs(float(o.get("length") or 0) - length) < 0.01 and o.get("units") == u]
                opt_id = new[-1].get("id") if new else None
            if not opt_id:
                return {**out, "isError": True, "error_code": "API_ERROR", "message": "建了選項但讀不到它的 id"}
            out["created_option"] = opt_id
        rp = await client.put(f"{base}/defaultoption/{opt_id}", json=None)
        if rp.is_error:
            return {**out, "isError": True, "error_code": "API_ERROR", "message": f"set default: {rp.message}"}
        r1 = await client.get(base)
        after = r1.data if isinstance(r1.data, dict) else {}
        aopt = next((o for o in after.get("options") or [] if o.get("id") == after.get("defaultId")), None)
        ok = (aopt is not None and abs(float(aopt.get("length") or 0) - length) < 0.01 and aopt.get("units") == u)
        out["after"] = {"defaultId": after.get("defaultId"), "default": aopt}
        if not ok:
            return {**out, "isError": True, "error_code": "WRITE_NOT_LANDED",
                    "message": "讀回的預設池長不是目標值"}
        return {**out, "success": True,
                "note": "只改之後新游泳課的預設；已上傳的活動池長要在該筆活動裡改（TP 不回溯）"}


LO_SETTINGS_TOOLS = ("lo_settings_audit", "lo_set_pool_length")


def register_lo_settings(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool
    tools.append(Tool(name="lo_settings_audit", description=(
        "Read-only roster settings check (HR anchors, general vs sport thresholds, zone sets, pool in yards, "
        "basic accounts); no athletes = all."),
        input_schema={"type": "object", "properties": {
            "athletes": {"type": "array", "items": {"type": "string"}},
            "save_to": {"type": "string"}}}))
    tools.append(Tool(name="lo_set_pool_length", description=(
        "Set default pool length (verified). Ask the coach first."),
        input_schema={"type": "object", "properties": {
            "length": {"type": "number"},
            "units": {"type": "string", "enum": ["Meters", "Yards"], "default": "Meters"},
            "dry_run": {"type": "boolean", "default": False}}, "required": ["length"]}))

    async def _h_audit(a):
        return await lo_settings_audit(a.get("athletes"), a.get("save_to"),
                                       int(a.get("concurrency", 3)), bool(a.get("include_self", False)))
    async def _h_pool(a): return await lo_set_pool_length(a["length"], a.get("units", "Meters"),
                                                          bool(a.get("dry_run", False)))
    handlers.update({"lo_settings_audit": _h_audit, "lo_set_pool_length": _h_pool})
