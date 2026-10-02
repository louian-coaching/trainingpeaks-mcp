"""FORK (羅教練 2026/09/27): StrongTri（強者之心）代理工具 lo_sth_*.

為什麼放在這個 MCP 裡：STH 的官方 MCP 在遠端（ai.strongtri.com），讀不到教練 Mac 上的檔案，
所以整週 classesJson（~33KB）要由模型逐字送三遍（試算、dry-run、真寫），每週 STH 呼叫 40–50 次。
這個 server 跑在教練的 Mac 上：讀本機 payload 檔、在 server 端逐堂呼叫遠端 STH，
模型只收摘要——與 TP 端 `tp_create_workouts_batch(payload_file)` 同一個思路（健檢 2026/09/27 方案 C2）。

連線設定：預設讀專案資料夾的 strongtri plugin 設定
  ~/Claude/Projects/TP 訓練助理/strongtri-plugin/.mcp.json（mcpServers.strongtri.url／headers.Authorization）
可用環境變數 STH_MCP_CONFIG 指到別的 .mcp.json。Token 不寫進本 repo（public fork）。

平台規則照 method/8 與 system/sth-ops-card.md：
  PLAT-10 距離制跑課取官方負荷＝capacity 暫改 time 再試算；PLAT-17 騎車 sport_type=CYCLE；
  PLAT-18 sentVsStoredDiverged 不當失敗；PLAT-27 一律帶 tri_user_id；PLAT-30 reorder 撞 409001 要等；
  PLAT-36 身分是 R 先切 C。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any

import httpx

DEFAULT_CONFIG = "~/Claude/Projects/TP 訓練助理/strongtri-plugin/.mcp.json"
PROTOCOL_VERSION = "2025-06-18"
CONFIRM_CREATE = "CONFIRM_SCHEDULE_CREATE"
CONFIRM_REORDER = "CONFIRM_SCHEDULE_REORDER_DAY"


# ---------------------------------------------------------------------------
# 最小 streamable-http MCP client（不依賴 SDK 版本；JSON 或 SSE 回應都吃）
# ---------------------------------------------------------------------------
def load_config(path: str | None = None) -> tuple[str, dict[str, str]]:
    p = Path(os.path.expanduser(path or os.environ.get("STH_MCP_CONFIG") or DEFAULT_CONFIG))
    cfg = json.loads(p.read_text(encoding="utf-8"))
    srv = cfg.get("mcpServers", {}).get("strongtri") or next(iter(cfg.get("mcpServers", {}).values()))
    return srv["url"], dict(srv.get("headers") or {})


def _parse_rpc(resp: httpx.Response, want_id: int) -> dict[str, Any]:
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for block in resp.text.split("\n\n"):
            data = "\n".join(l[5:].lstrip() for l in block.splitlines() if l.startswith("data:"))
            if not data:
                continue
            msg = json.loads(data)
            if msg.get("id") == want_id:
                return msg
        raise RuntimeError("STH SSE 回應裡沒有對應的 JSON-RPC id")
    return resp.json()


class StrongTriClient:
    def __init__(self, url: str, headers: dict[str, str], timeout: float = 120.0):
        self.url = url
        self.headers = {**headers, "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json"}
        self.timeout = timeout
        self._id = 0
        self._session: str | None = None
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "StrongTriClient":
        self._http = httpx.AsyncClient(timeout=self.timeout)
        await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "tp-mcp-lo-sth", "version": "1"},
        })
        await self._notify("notifications/initialized")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http:
            await self._http.aclose()

    def _hdr(self) -> dict[str, str]:
        h = dict(self.headers)
        if self._session:
            h["Mcp-Session-Id"] = self._session
        return h

    async def _notify(self, method: str) -> None:
        assert self._http
        await self._http.post(self.url, headers=self._hdr(),
                              json={"jsonrpc": "2.0", "method": method})

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self._http
        self._id += 1
        r = await self._http.post(self.url, headers=self._hdr(),
                                  json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if r.status_code >= 400:
            raise RuntimeError(f"STH HTTP {r.status_code}: {r.text[:300]}")
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._session = sid
        msg = _parse_rpc(r, self._id)
        if "error" in msg:
            raise RuntimeError(f"STH JSON-RPC error: {json.dumps(msg['error'], ensure_ascii=False)[:300]}")
        return msg.get("result", {})

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        res = await self._rpc("tools/call", {"name": tool, "arguments": arguments})
        return unwrap(res)


def unwrap(res: dict[str, Any]) -> dict[str, Any]:
    """MCP tools/call result → 平台原本的 JSON 物件。"""
    if isinstance(res.get("structuredContent"), dict):
        out = res["structuredContent"]
        if set(out) == {"result"}:
            inner = out["result"]
            # 2026/10/02：STH 開始把 result 包成 JSON 字串（{"result": "{...}"}），要再解一層
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except (json.JSONDecodeError, TypeError):
                    pass
            if isinstance(inner, dict):
                out = inner
        return out
    texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
    raw = "\n".join(texts)
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except (json.JSONDecodeError, TypeError):
        return {"text": raw, "isError": bool(res.get("isError"))}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def find_key(obj: Any, key: str) -> Any:
    """深度優先找第一個 key（平台回傳巢狀層級會變，不綁死路徑）。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            hit = find_key(v, key)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find_key(v, key)
            if hit is not None:
                return hit
    return None


def blocking_of(res: dict[str, Any]) -> list[Any]:
    b = find_key(res, "blocking")
    if isinstance(b, list):
        return b
    return [b] if b else []


def is_ok(res: dict[str, Any]) -> bool:
    if res.get("isError"):
        return False
    gate = find_key(res, "postCreateQualityGate")
    if isinstance(gate, dict) and "ok" in gate:
        return bool(gate["ok"]) and not blocking_of(res)
    ok = res.get("ok")
    return (ok is not False) and not blocking_of(res)


def _resolve_file_args(args: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and v.startswith("@file:"):
            out[k] = Path(os.path.expanduser(v[6:])).read_text(encoding="utf-8")
        else:
            out[k] = v
    return out


def _dump(path: str, data: Any) -> str:
    p = Path(os.path.expanduser(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(p)


def _cj(w: dict[str, Any]) -> dict[str, Any] | None:
    cj = w.get("classes_json")
    if isinstance(cj, str) and cj.strip():
        return json.loads(cj)
    return cj if isinstance(cj, dict) else None


def _cj_str(cj: dict[str, Any] | None) -> str:
    return "" if cj is None else json.dumps(cj, ensure_ascii=False, separators=(",", ":"))


def load_payload(path: str) -> dict[str, Any]:
    p = json.loads(Path(os.path.expanduser(path)).read_text(encoding="utf-8"))
    if not p.get("tri_user_id"):
        raise ValueError("payload 缺 tri_user_id（PLAT-27：一律帶 triUserId，不用 athleteRef）")
    if not isinstance(p.get("workouts"), list) or not p["workouts"]:
        raise ValueError("payload.workouts 必須是非空陣列")
    for i, w in enumerate(p["workouts"]):
        for k in ("classes_date", "sport_type", "title", "duration_min"):
            if k not in w:
                raise ValueError(f"workouts[{i}] 缺 {k}")
        if not isinstance(w["duration_min"], int):
            raise ValueError(f"workouts[{i}].duration_min 必須是整數（PLAT-26／28）")
    return p


def distance_to_time(cj: dict[str, Any]) -> dict[str, Any]:
    """PLAT-10：距離制段暫改 capacity=time（targetSeconds 不變）才算得出官方負荷。"""
    c = copy.deepcopy(cj)
    for st in c.get("stages", []) or []:
        for sec in st.get("sections", []) or []:
            if sec.get("capacity") == "distance":
                sec["capacity"] = "time"
    return c


# ---------------------------------------------------------------------------
# 工具本體（client_factory 可注入假 client 給測試用）
# ---------------------------------------------------------------------------
def _default_factory() -> StrongTriClient:
    url, headers = load_config()
    return StrongTriClient(url, headers)


async def _ensure_coach(cli: StrongTriClient) -> dict[str, Any]:
    ident = await cli.call("get_current_identity", {})
    itype = find_key(ident, "identityType")
    switched = False
    if itype and itype != "C":
        await cli.call("switch_active_identity", {"identity_type": "C"})  # PLAT-36
        ident = await cli.call("get_current_identity", {})
        itype = find_key(ident, "identityType")
        switched = True
    return {"identityType": itype, "displayName": find_key(ident, "displayName"), "switched": switched}


async def lo_sth_call(tool: str, arguments: dict[str, Any] | None = None, arguments_file: str | None = None,
                      save_to: str | None = None, client_factory=_default_factory) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if arguments_file:
        args.update(json.loads(Path(os.path.expanduser(arguments_file)).read_text(encoding="utf-8")))
    args.update(arguments or {})
    args = _resolve_file_args(args)
    async with client_factory() as cli:
        res = await cli.call(tool, args)
    if save_to:
        return {"saved_to": _dump(save_to, res), "tool": tool, "ok": is_ok(res),
                "blocking": blocking_of(res)[:5], "top_level_keys": list(res)[:15]}
    return res


def stages_sig(cj: dict[str, Any] | None) -> str:
    """結構指紋：sth_build.save_payload 靠它判斷「這堂結構沒變，可以沿用官方負荷」。"""
    import hashlib
    raw = json.dumps((cj or {}).get("stages") or [], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def write_loads_sidecar(payload_file: str, p: dict[str, Any]) -> str:
    """優化 9：官方負荷另存 <payload>.loads.json（key＝日期|項目|標題），建課腳本重跑時照結構指紋取回，
    不會再被 sth=0 蓋掉；同時記 _classScheduleId，重跑後 reorder／finish 還找得到課。"""
    f = Path(os.path.expanduser(payload_file))
    side = f.with_name(f.stem + ".loads.json")
    old = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
    for w in p["workouts"]:
        cj = _cj(w) or {}
        key = f"{w['classes_date']}|{str(w['sport_type']).upper()}|{w['title']}"
        ent = dict(old.get(key) or {})
        if cj.get("stl"):
            ent.update({"sth": cj.get("sth"), "stl": cj.get("stl"), "sig": stages_sig(cj)})
        if w.get("_classScheduleId"):
            ent["_classScheduleId"] = w["_classScheduleId"]
        if ent:
            old[key] = ent
    side.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(side)


def week_totals(p: dict[str, Any]) -> dict[str, Any]:
    """payload 內每項目的 STL 合計（騎跑官方值；游泳／肌力用 payload 裡已估的值）。"""
    by: dict[str, int] = {}
    for w in p["workouts"]:
        cj = _cj(w) or {}
        sp = "RIDE" if str(w["sport_type"]).upper() in ("RIDE", "CYCLE") else str(w["sport_type"]).upper()
        by[sp] = by.get(sp, 0) + int(cj.get("stl") or 0)
    return by


def band_check(by: dict[str, int], band: list[int] | None, sports: list[str] | None) -> dict[str, Any] | None:
    if not band:
        return None
    use = [s.upper().replace("CYCLE", "RIDE") for s in (sports or ["RUN", "RIDE", "SWIM"])]
    tot = sum(v for k, v in by.items() if k in use)
    lo, hi = int(band[0]), int(band[1])
    return {"sports": use, "total": tot, "band": [lo, hi],
            "status": "in" if lo <= tot <= hi else ("under" if tot < lo else "over"),
            "delta_to_band": 0 if lo <= tot <= hi else (lo - tot if tot < lo else hi - tot)}


async def lo_sth_calc_loads(payload_file: str, overwrite: bool = False,
                            client_factory=_default_factory, band: list[int] | None = None,
                            band_sports: list[str] | None = None) -> dict[str, Any]:
    p = load_payload(payload_file)
    rows = []
    async with client_factory() as cli:
        for i, w in enumerate(p["workouts"]):
            sport = str(w["sport_type"]).upper()
            cj = _cj(w)
            if sport not in ("RUN", "RIDE", "CYCLE") or cj is None:
                rows.append({"i": i, "date": w["classes_date"], "title": w["title"], "skipped": "非騎跑或無 classes_json"})
                continue
            if cj.get("sth") and not overwrite:
                rows.append({"i": i, "date": w["classes_date"], "title": w["title"], "kept": [cj.get("sth"), cj.get("stl")]})
                continue
            probe = distance_to_time(cj)
            res = await cli.call("calculate_workout_sth", {
                "classes_json": _cj_str(probe), "sport_type": "CYCLE" if sport in ("RIDE", "CYCLE") else "RUN",
                "tri_user_id": p["tri_user_id"], "classes_date": w["classes_date"]})
            sth, stl = find_key(res, "sth"), find_key(res, "stl")
            if isinstance(sth, (int, float)) and sth > 0 and isinstance(stl, (int, float)):
                cj["sth"], cj["stl"] = int(round(sth)), int(round(stl))
                w["classes_json"] = cj
                row = {"i": i, "date": w["classes_date"], "title": w["title"], "sth": cj["sth"], "stl": cj["stl"]}
                # 優化 3：官方值離本機估算太遠（±25%）就標出來，別直接收
                from tp_mcp.tools.lo_sth_ext import est_stl
                est = est_stl({**cj, "stl": 0})
                if est and abs(cj["stl"] - est) / est > 0.25:
                    row["warning"] = {"code": "LOAD_FAR_FROM_ESTIMATE", "estimate": est}
                rows.append(row)
            else:
                rows.append({"i": i, "date": w["classes_date"], "title": w["title"], "error": "官方未回可用負荷",
                             "detail": {k: res.get(k) for k in list(res)[:6]}})
    Path(os.path.expanduser(payload_file)).write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    side = write_loads_sidecar(payload_file, p)
    ratios = [r["sth"] / r["stl"] for r in rows if r.get("stl")]
    by = week_totals(p)
    for r in rows:  # 每分鐘 STL：調量時直接換算要砍／加幾分鐘
        w = p["workouts"][r["i"]]
        if r.get("stl") and w.get("duration_min"):
            r["stl_per_min"] = round(r["stl"] / int(w["duration_min"]), 2)
    return {"payload_file": payload_file, "rows": rows, "totals_by_sport": by,
            **({"band": band_check(by, band, band_sports)} if band else {}),
            "sth_to_stl_measured": round(sum(ratios) / len(ratios), 1) if ratios else None,
            "loads_sidecar": side,
            "note": "sth_to_stl_measured＝同批騎跑官方實測比值，游泳／肌力估算 STH 用它（PLAT-15）"}


def _create_args(p: dict[str, Any], w: dict[str, Any]) -> dict[str, Any]:
    sport = str(w["sport_type"]).upper()
    return {
        "classes_date": w["classes_date"], "sport_type": "RIDE" if sport == "CYCLE" else sport,
        "duration_min": int(w["duration_min"]), "title": w["title"],
        "classes_json": _cj_str(_cj(w)), "distance": w.get("distance", 0) or 0,
        "identity_type": "C", "tri_user_id": p["tri_user_id"],
        **({"classes_id": w["classes_id"]} if w.get("classes_id") else {}),
    }


def _sport_key(v: Any) -> str:
    v = str(v or "").upper()
    return "RIDE" if v in ("CYCLE", "RIDE") else v


def _save_payload(payload_file: str, p: dict[str, Any]) -> None:
    Path(os.path.expanduser(payload_file)).write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")


async def lo_sth_create_week(payload_file: str, write: bool = False, readback_save_to: str | None = None,
                             client_factory=_default_factory, finish: bool = False, sleep=asyncio.sleep,
                             settle_s: int = 45, band: list[int] | None = None,
                             band_sports: list[str] | None = None, allow_duplicates: bool = False,
                             clock=None) -> dict[str, Any]:
    """10/02 優化 2：①建課前先比對平台同日同項目同標題的既有課，有就跳過（create_workout 沒有冪等鍵，
    逾時後重送不會重複建）②每建好一堂就把 _classScheduleId 寫回 payload（中途斷線也不會遺失）
    ③finish 不再在同一呼叫裡等 45 秒（會超過裝置 60 秒逾時）——寫入夠快才順手收尾，否則回 next 叫
    lo_sth_finish_week。"""
    import time as _t
    clock = clock or _t.monotonic
    t0 = clock()
    p = load_payload(payload_file)
    out: dict[str, Any] = {"tri_user_id": p["tri_user_id"], "mode": "write" if write else "dry_run", "rows": []}
    dates = sorted(w["classes_date"] for w in p["workouts"])
    async with client_factory() as cli:
        out["identity"] = await _ensure_coach(cli)
        if out["identity"]["identityType"] != "C":
            return {**out, "isError": True, "error_code": "IDENTITY_NOT_COACH",
                    "message": "身分不是教練（C），停止（method/8 §8.1）"}
        from tp_mcp.tools.lo_sth_ext import iter_week, save_snapshot
        wk0 = await cli.call("get_week_schedule", {"date_from": dates[0], "date_to": dates[-1],
                                                   "tri_user_id": p["tri_user_id"]})
        on_platform = {w.get("classScheduleId"): (d, _sport_key(w.get("sportType")), (w.get("title") or "").strip())
                       for d, w in iter_week(wk0)}
        claimed = {w.get("_classScheduleId") for w in p["workouts"] if w.get("_classScheduleId") in on_platform}
        todo: list[int] = []
        pre: dict[int, dict[str, Any]] = {}
        for i, w in enumerate(p["workouts"]):
            if w.get("_classScheduleId") in on_platform:
                pre[i] = {"status": "already_created", "classScheduleId": w["_classScheduleId"]}
                continue
            key = (w["classes_date"], _sport_key(w["sport_type"]), (w["title"] or "").strip())
            hit = next((cid for cid, k in on_platform.items() if k == key and cid not in claimed), None)
            if hit is not None and not allow_duplicates:
                claimed.add(hit)
                w["_classScheduleId"] = hit
                pre[i] = {"status": "skipped_existing", "classScheduleId": hit,
                          "note": "平台已有同日同項目同標題的課，不重複建立（allow_duplicates=true 才建）"}
                continue
            todo.append(i)
        if pre:
            _save_payload(payload_file, p)
        # ① 全部 dry-run，任何一堂有 blocking 就整批不寫
        dry = []
        for i in todo:
            w = p["workouts"][i]
            res = await cli.call("create_workout", {**_create_args(p, w), "dry_run": True})
            dry.append({"i": i, "date": w["classes_date"], "title": w["title"],
                        "sport": w["sport_type"], "blocking": blocking_of(res)[:5],
                        "error": res.get("text") if res.get("isError") else None})
        bad = [d for d in dry if d["blocking"] or d["error"]]
        pre_rows = [{"i": i, "date": p["workouts"][i]["classes_date"], "title": p["workouts"][i]["title"],
                     "sport": p["workouts"][i]["sport_type"], **v} for i, v in sorted(pre.items())]
        if bad or not write:
            out["rows"] = sorted(dry + pre_rows, key=lambda r: r["i"])
            if bad:
                out.update({"isError": True, "error_code": "DRY_RUN_BLOCKED",
                            "message": f"{len(bad)} 堂 dry-run 有 blocking，整批未寫入"})
            return out
        # ② 真寫（循序、不重試；有一堂沒過就停，其餘標 not_attempted）；每堂寫完立刻存 payload
        stop = False
        rows = list(pre_rows)
        for i in todo:
            w = p["workouts"][i]
            row: dict[str, Any] = {"i": i, "date": w["classes_date"], "title": w["title"], "sport": w["sport_type"]}
            if stop:
                row["status"] = "not_attempted"
            else:
                res = await cli.call("create_workout", {**_create_args(p, w), "dry_run": False,
                                                          "confirm_write": True, "confirm_token": CONFIRM_CREATE})
                row["classScheduleId"] = find_key(res, "classScheduleId")
                row["readbackOk"] = find_key(res, "readbackOk")
                row["ok"] = is_ok(res)
                row["status"] = "created" if row["ok"] else "failed"
                if row["classScheduleId"]:
                    w["_classScheduleId"] = row["classScheduleId"]
                    _save_payload(payload_file, p)
                if not row["ok"]:
                    row["blocking"] = blocking_of(res)[:5]
                    row["raw_head"] = json.dumps(res, ensure_ascii=False)[:400]
                    stop = True
            rows.append(row)
        out["rows"] = sorted(rows, key=lambda r: r["i"])
        # ③ 讀回整週、存快照
        wk = await cli.call("get_week_schedule", {"date_from": dates[0], "date_to": dates[-1],
                                                  "tri_user_id": p["tri_user_id"]})
        if readback_save_to:
            out["readback_saved_to"] = _dump(readback_save_to, wk)
        vers = {w.get("classScheduleId"): w.get("scheduleVersion") for _, w in iter_week(wk)}
        for r in out["rows"]:
            if r.get("status") == "created" and r.get("classScheduleId"):
                w = p["workouts"][r["i"]]
                save_snapshot(p["tri_user_id"], r["classScheduleId"], _cj(w), w["classes_date"],
                              vers.get(r["classScheduleId"]), w["title"], "create_week")
        _save_payload(payload_file, p)
    write_loads_sidecar(payload_file, p)
    ok_states = ("created", "already_created", "skipped_existing")
    n_ok = sum(r.get("status") in ok_states for r in out["rows"])
    n_new = sum(r.get("status") == "created" for r in out["rows"])
    out["summary"] = f"{n_ok}/{len(p['workouts'])} 堂在平台上（本次新建 {n_new}）"
    if n_ok != len(p["workouts"]):
        out.update({"isError": True, "error_code": "WRITE_INCOMPLETE"})
        return out
    out["elapsed_s"] = round(clock() - t0, 1)
    if finish and out["elapsed_s"] <= 10:
        fin = await lo_sth_finish_week(payload_file, band=band, band_sports=band_sports,
                                       readback_save_to=readback_save_to, client_factory=client_factory,
                                       sleep=sleep, time_budget_s=max(0, 45 - int(out["elapsed_s"])))
        out["finish"] = fin
        return out
    out["next"] = ("呼叫 lo_sth_finish_week(payload_file, band=…)：排序（含當天既有課）→ validate → 讀回 → 各項 STL。"
                   "寫入後馬上排序常撞 409001（裝置同步中），它會自己等、等不到就回 pending 再叫一次")
    return out


async def lo_sth_finish_week(payload_file: str, band: list[int] | None = None, band_sports: list[str] | None = None,
                             readback_save_to: str | None = None, client_factory=_default_factory,
                             sleep=asyncio.sleep, time_budget_s: int = 45, retry_wait_s: int = 15) -> dict[str, Any]:
    """建完整週後收尾（10/02 優化 2）：排序→validate_schedule_week→讀回（排序之後）→各項 STL／比帶。
    排序撞 409001 會在 time_budget_s 內等；等不到回 status=pending_device_sync，過一會再叫一次（可重複呼叫）。"""
    from tp_mcp.tools.lo_sth_ext import iter_week as _iw
    p = load_payload(payload_file)
    dates = sorted(w["classes_date"] for w in p["workouts"])
    ro = await lo_sth_reorder_week(payload_file, retry_wait_s=retry_wait_s, retries=99, client_factory=client_factory,
                                   sleep=sleep, include_existing=True, time_budget_s=time_budget_s)
    out: dict[str, Any] = {"reorder": ro.get("rows", [])}
    if any(r.get("status") == "pending_sync" for r in out["reorder"]):
        return {**out, "status": "pending_device_sync",
                "next": "裝置還在同步：等 30 秒再呼叫一次 lo_sth_finish_week（已排好的日子會略過不動）"}
    async with client_factory() as cli:
        v = await cli.call("validate_schedule_week", {"tri_user_id": p["tri_user_id"], "date": dates[0]})
        wk2 = await cli.call("get_week_schedule", {"date_from": dates[0], "date_to": dates[-1],
                                                   "tri_user_id": p["tri_user_id"]})
    if readback_save_to:
        out["readback_saved_to"] = _dump(readback_save_to, wk2)
    issues = [i for i in (v.get("issues") or []) if i.get("severity") not in ("info",)]
    by: dict[str, int] = {}
    order: dict[str, list[str]] = {}
    for d, w in _iw(wk2):
        sp = _sport_key(w.get("sportType"))
        by[sp] = by.get(sp, 0) + int(w.get("plannedSTL") or 0)
        order.setdefault(d, []).append(w.get("title"))
    out["status"] = "done"
    out["validate"] = {"range": [v.get("beginDay"), v.get("endDay")], "scheduleCount": v.get("scheduleCount"),
                       "error_warning": issues[:10], "ok": not issues and v.get("statsConsistent", True)}
    out["day_order"] = {d: t for d, t in order.items() if len(t) > 1}
    out["week_stl_by_sport"] = by
    if band:
        out["band"] = band_check(by, band, band_sports)
    return out


def _day_ids(week: Any, date: str) -> list[Any]:
    """從 get_week_schedule 回傳裡撈出某日全部 classScheduleId（依現有顯示順序）。"""
    found: list[tuple[int, Any]] = []

    def walk(o: Any, day: str | None = None) -> None:
        if isinstance(o, dict):
            d = str(o.get("classesDate") or o.get("classes_date") or o.get("date") or day or "")[:10]
            if "classScheduleId" in o and d == date:
                found.append((int(o.get("sort") or o.get("displayOrder") or 0), o["classScheduleId"]))
            for k, v in o.items():
                walk(v, k if isinstance(k, str) and k[:4].isdigit() else d or day)
        elif isinstance(o, list):
            for v in o:
                walk(v, day)
    walk(week)
    seen, ids = set(), []
    for _, i in sorted(found, key=lambda t: t[0]):
        if i not in seen:
            seen.add(i)
            ids.append(i)
    return ids


async def lo_sth_reorder_week(payload_file: str, retry_wait_s: int = 20, retries: int = 2,
                              client_factory=_default_factory, sleep=asyncio.sleep,
                              include_existing: bool = False, time_budget_s: int | None = None) -> dict[str, Any]:
    p = load_payload(payload_file)
    by_day: dict[str, list[Any]] = {}
    for w in p["workouts"]:
        if w.get("_classScheduleId"):
            by_day.setdefault(w["classes_date"], []).append(w["_classScheduleId"])
    days = dict(by_day) if include_existing else {d: ids for d, ids in by_day.items() if len(ids) > 1}
    rows = []
    if not days:
        return {"rows": [], "note": "沒有同日多堂（或 payload 沒有 _classScheduleId，先跑 lo_sth_create_week write=true）"}
    waited = 0
    async with client_factory() as cli:
        for d, ours in sorted(days.items()):
            wk = await cli.call("get_week_schedule", {"date_from": d, "date_to": d, "tri_user_id": p["tri_user_id"]})
            current = _day_ids(wk, d)
            existing = [i for i in current if i not in ours]
            if include_existing and not existing and len(ours) < 2:
                continue  # 單堂、當天沒別的課：不用排
            order = existing + ours  # 教練既有課在前、新課照 payload 順序
            if current == order:
                rows.append({"date": d, "order": order, "status": "already_ordered"})
                continue
            args = {"classes_date": d, "ordered_schedule_ids": json.dumps(order), "tri_user_id": p["tri_user_id"]}
            status, last = "failed", {}
            attempt = 0
            while True:
                last = await cli.call("reorder_day", {**args, "dry_run": False, "confirm_write": True,
                                                      "confirm_token": CONFIRM_REORDER})
                txt = json.dumps(last, ensure_ascii=False)
                if "409001" in txt:
                    budget_left = (time_budget_s - waited) if time_budget_s is not None else None
                    if attempt < retries and (budget_left is None or budget_left >= retry_wait_s):
                        await sleep(retry_wait_s)  # PLAT-30：裝置同步中
                        waited += retry_wait_s
                        attempt += 1
                        continue
                    status = "pending_sync"
                    break
                status = "applied" if find_key(last, "orderApplied") or is_ok(last) else "failed"
                break
            rows.append({"date": d, "order": order, "status": status,
                         **({} if status in ("applied", "pending_sync") else
                            {"raw_head": json.dumps(last, ensure_ascii=False)[:300]})})
    bad = [r for r in rows if r["status"] not in ("applied", "already_ordered")]
    return {"rows": rows, **({"isError": True, "error_code": "REORDER_INCOMPLETE"} if bad else {})}


# ---------------------------------------------------------------------------
# 註冊
# ---------------------------------------------------------------------------
LO_STH_TOOLS = ("lo_sth_call", "lo_sth_calc_loads", "lo_sth_create_week", "lo_sth_reorder_week", "lo_sth_finish_week")

_PF = {"type": "string", "description": (
    "Absolute path ON THE COACH'S MAC to the week payload JSON: "
    '{"tri_user_id":"…","workouts":[{"classes_date":"YYYY-MM-DD","sport_type":"RUN|RIDE|SWIM|STRENGTH|REST",'
    '"title":"…","duration_min":60,"distance":0,"classes_json":{…sth_build.py output…}}]}')}


def register_lo_sth(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(Tool(name="lo_sth_call", description=(
        "StrongTri 代理：在教練 Mac 上呼叫任一遠端 STH 工具。大參數放檔案——`arguments_file`（JSON 物件）或任何字串值寫 "
        "`@file:/abs/path` 會被換成該檔內容；大回傳用 `save_to` 落檔只回摘要。用在 classesJson 很長的 "
        "update_assigned_workout／calculate_workout_sth 等單堂呼叫，避免模型逐字重送。"),
        input_schema={"type": "object", "properties": {
            "tool": {"type": "string", "description": "STH 工具名，例如 update_assigned_workout"},
            "arguments": {"type": "object", "description": "直接給的參數（會覆蓋 arguments_file 同名鍵）"},
            "arguments_file": {"type": "string", "description": "Mac 上的 JSON 參數檔"},
            "save_to": {"type": "string", "description": "Mac 上的輸出路徑；有給就只回摘要"},
        }, "required": ["tool"]}))
    tools.append(Tool(name="lo_sth_calc_loads", description=(
        "對 payload 裡每堂騎跑課呼叫官方 calculate_workout_sth，把 sth/stl 回填進 payload 檔（原地改寫）。"
        "距離制跑課自動暫改 capacity=time 試算（PLAT-10）、騎車自動用 CYCLE（PLAT-17）；已有 sth 的課略過（overwrite=true 才重算）。"
        "回傳逐堂 sth/stl 與同批實測 sth_to_stl（游泳／肌力估算用，PLAT-15）。"),
        input_schema={"type": "object", "properties": {
            "payload_file": _PF, "overwrite": {"type": "boolean", "default": False},
            "band": {"type": "array", "items": {"type": "integer"}, "description": "量級帶 [下限, 上限]，回 status in/under/over 與差多少"},
            "band_sports": {"type": "array", "items": {"type": "string"}, "description": "量級帶算哪幾項，預設 RUN／RIDE／SWIM（騎跑選手給 [\"RUN\",\"RIDE\"]）"}},
            "required": ["payload_file"]}))
    tools.append(Tool(name="lo_sth_create_week", description=(
        "StrongTri 整週建課：先確認教練身分（R 自動切 C，PLAT-36）→ 全部 dry-run（任一堂有 blocking 整批不寫）→ "
        "write=true 時逐堂三件套真寫（CONFIRM_SCHEDULE_CREATE），每建好一堂就把 classScheduleId 寫回 payload → 讀回整週（readback_save_to 落檔）。"
        "預設 write=false＝只 dry-run。⚡ 防重複：平台上已有同日同項目同標題的課（或 payload 已記 _classScheduleId 的課）一律跳過、不重建"
        "（allow_duplicates=true 才建）——逾時後原樣重送是安全的。`finish=true`：寫入 ≤10 秒才順手收尾，否則回 next 叫 lo_sth_finish_week"
        "（不在同一呼叫裡等 45 秒，避免超過裝置 60 秒逾時）。成功建立的課自動存快照（給 lo_sth_diff_week）。"),
        input_schema={"type": "object", "properties": {
            "payload_file": _PF, "write": {"type": "boolean", "default": False},
            "readback_save_to": {"type": "string", "description": "Mac 上的整週讀回輸出路徑"},
            "finish": {"type": "boolean", "default": False},
            "allow_duplicates": {"type": "boolean", "default": False},
            "band": {"type": "array", "items": {"type": "integer"}},
            "band_sports": {"type": "array", "items": {"type": "string"}}},
            "required": ["payload_file"]}))
    tools.append(Tool(name="lo_sth_finish_week", description=(
        "建完整週後收尾（10/02 優化 2）：同日排序（含當天教練既有課，既有在前；已排好的日子略過）→ validate_schedule_week → "
        "排序之後才讀回整週 → 各項 STL（帶 band 就比帶）＋每天的課順序。排序撞 409001 會在 time_budget_s（預設 45）內等；"
        "等不到回 status=pending_device_sync，過 30 秒再叫一次即可（可重複呼叫）。"),
        input_schema={"type": "object", "properties": {
            "payload_file": _PF, "readback_save_to": {"type": "string", "description": "Mac 上的整週讀回輸出路徑（排序之後）"},
            "band": {"type": "array", "items": {"type": "integer"}},
            "band_sports": {"type": "array", "items": {"type": "string"}},
            "time_budget_s": {"type": "integer", "default": 45}}, "required": ["payload_file"]}))
    tools.append(Tool(name="lo_sth_reorder_week", description=(
        "照 payload 順序排同日多堂（reorder_day 真寫）；教練既有課排前、新課照 payload 順序。撞 409001（裝置同步中）自動等 "
        "retry_wait_s 秒重試（PLAT-30）。先跑過 lo_sth_create_week write=true。"),
        input_schema={"type": "object", "properties": {
            "payload_file": _PF, "retry_wait_s": {"type": "integer", "default": 20},
            "retries": {"type": "integer", "default": 2},
            "time_budget_s": {"type": "integer", "description": "總共最多等幾秒；用完還是 409001 就回 pending_sync"}},
            "required": ["payload_file"]}))

    async def _h_call(a): return await lo_sth_call(a["tool"], a.get("arguments"), a.get("arguments_file"), a.get("save_to"))
    async def _h_calc(a): return await lo_sth_calc_loads(a["payload_file"], bool(a.get("overwrite", False)),
                                                         band=a.get("band"), band_sports=a.get("band_sports"))
    async def _h_create(a): return await lo_sth_create_week(a["payload_file"], bool(a.get("write", False)), a.get("readback_save_to"),
                                                            finish=bool(a.get("finish", False)), band=a.get("band"),
                                                            band_sports=a.get("band_sports"),
                                                            allow_duplicates=bool(a.get("allow_duplicates", False)))
    async def _h_reorder(a): return await lo_sth_reorder_week(a["payload_file"], int(a.get("retry_wait_s", 20)), int(a.get("retries", 2)),
                                                              time_budget_s=a.get("time_budget_s"))
    async def _h_finish(a): return await lo_sth_finish_week(a["payload_file"], band=a.get("band"), band_sports=a.get("band_sports"),
                                                            readback_save_to=a.get("readback_save_to"),
                                                            time_budget_s=int(a.get("time_budget_s", 45)))

    handlers.update({"lo_sth_call": _h_call, "lo_sth_calc_loads": _h_calc,
                     "lo_sth_create_week": _h_create, "lo_sth_reorder_week": _h_reorder,
                     "lo_sth_finish_week": _h_finish})
