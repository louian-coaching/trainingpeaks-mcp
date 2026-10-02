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


async def lo_sth_calc_loads(payload_file: str, overwrite: bool = False,
                            client_factory=_default_factory) -> dict[str, Any]:
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
                rows.append({"i": i, "date": w["classes_date"], "title": w["title"], "sth": cj["sth"], "stl": cj["stl"]})
            else:
                rows.append({"i": i, "date": w["classes_date"], "title": w["title"], "error": "官方未回可用負荷",
                             "detail": {k: res.get(k) for k in list(res)[:6]}})
    Path(os.path.expanduser(payload_file)).write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    ratios = [r["sth"] / r["stl"] for r in rows if r.get("stl")]
    return {"payload_file": payload_file, "rows": rows,
            "sth_to_stl_measured": round(sum(ratios) / len(ratios), 1) if ratios else None,
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


async def lo_sth_create_week(payload_file: str, write: bool = False, readback_save_to: str | None = None,
                             client_factory=_default_factory) -> dict[str, Any]:
    p = load_payload(payload_file)
    out: dict[str, Any] = {"tri_user_id": p["tri_user_id"], "mode": "write" if write else "dry_run", "rows": []}
    async with client_factory() as cli:
        out["identity"] = await _ensure_coach(cli)
        if out["identity"]["identityType"] != "C":
            return {**out, "isError": True, "error_code": "IDENTITY_NOT_COACH",
                    "message": "身分不是教練（C），停止（method/8 §8.1）"}
        # ① 全部 dry-run，任何一堂有 blocking 就整批不寫
        dry = []
        for i, w in enumerate(p["workouts"]):
            res = await cli.call("create_workout", {**_create_args(p, w), "dry_run": True})
            dry.append({"i": i, "date": w["classes_date"], "title": w["title"],
                        "sport": w["sport_type"], "blocking": blocking_of(res)[:5],
                        "error": res.get("text") if res.get("isError") else None})
        bad = [d for d in dry if d["blocking"] or d["error"]]
        if bad or not write:
            out["rows"] = dry
            if bad:
                out.update({"isError": True, "error_code": "DRY_RUN_BLOCKED",
                            "message": f"{len(bad)} 堂 dry-run 有 blocking，整批未寫入"})
            return out
        # ② 真寫（循序、不重試；有一堂沒過就停，其餘標 not_attempted）
        stop = False
        for i, w in enumerate(p["workouts"]):
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
                if not row["ok"]:
                    row["blocking"] = blocking_of(res)[:5]
                    row["raw_head"] = json.dumps(res, ensure_ascii=False)[:400]
                    stop = True
            out["rows"].append(row)
        # ③ 讀回整週（排序另跑 lo_sth_reorder_week，避免這一呼叫被 PLAT-30 的等待拖太久）
        dates = sorted(w["classes_date"] for w in p["workouts"])
        wk = await cli.call("get_week_schedule", {"date_from": dates[0], "date_to": dates[-1],
                                                  "tri_user_id": p["tri_user_id"]})
        if readback_save_to:
            out["readback_saved_to"] = _dump(readback_save_to, wk)
        # 把新 id 回寫 payload，給 reorder 用
        for r in out["rows"]:
            if r.get("classScheduleId"):
                p["workouts"][r["i"]]["_classScheduleId"] = r["classScheduleId"]
        Path(os.path.expanduser(payload_file)).write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    n_ok = sum(r.get("status") == "created" for r in out["rows"])
    out["summary"] = f"{n_ok}/{len(p['workouts'])} 堂寫入"
    if n_ok != len(p["workouts"]):
        out.update({"isError": True, "error_code": "WRITE_INCOMPLETE"})
    multi = sorted({w["classes_date"] for w in p["workouts"]
                    if sum(x["classes_date"] == w["classes_date"] for x in p["workouts"]) > 1})
    if multi:
        out["next"] = f"同日多堂 {multi}：等 ~45 秒後呼叫 lo_sth_reorder_week(payload_file)（PLAT-30）"
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
                              client_factory=_default_factory, sleep=asyncio.sleep) -> dict[str, Any]:
    p = load_payload(payload_file)
    by_day: dict[str, list[Any]] = {}
    for w in p["workouts"]:
        if w.get("_classScheduleId"):
            by_day.setdefault(w["classes_date"], []).append(w["_classScheduleId"])
    days = {d: ids for d, ids in by_day.items() if len(ids) > 1}
    rows = []
    if not days:
        return {"rows": [], "note": "沒有同日多堂（或 payload 沒有 _classScheduleId，先跑 lo_sth_create_week write=true）"}
    async with client_factory() as cli:
        for d, ours in sorted(days.items()):
            wk = await cli.call("get_week_schedule", {"date_from": d, "date_to": d, "tri_user_id": p["tri_user_id"]})
            existing = [i for i in _day_ids(wk, d) if i not in ours]
            order = existing + ours  # 教練既有課在前、新課照 payload 順序
            args = {"classes_date": d, "ordered_schedule_ids": json.dumps(order), "tri_user_id": p["tri_user_id"]}
            status, last = "failed", {}
            for attempt in range(retries + 1):
                last = await cli.call("reorder_day", {**args, "dry_run": False, "confirm_write": True,
                                                      "confirm_token": CONFIRM_REORDER})
                txt = json.dumps(last, ensure_ascii=False)
                if "409001" in txt and attempt < retries:
                    await sleep(retry_wait_s)  # PLAT-30：裝置同步中
                    continue
                status = "applied" if find_key(last, "orderApplied") or is_ok(last) else "failed"
                break
            rows.append({"date": d, "order": order, "status": status,
                         **({} if status == "applied" else {"raw_head": json.dumps(last, ensure_ascii=False)[:300]})})
    return {"rows": rows, **({"isError": True, "error_code": "REORDER_INCOMPLETE"}
                             if any(r["status"] != "applied" for r in rows) else {})}


# ---------------------------------------------------------------------------
# 註冊
# ---------------------------------------------------------------------------
LO_STH_TOOLS = ("lo_sth_call", "lo_sth_calc_loads", "lo_sth_create_week", "lo_sth_reorder_week")

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
            "payload_file": _PF, "overwrite": {"type": "boolean", "default": False}}, "required": ["payload_file"]}))
    tools.append(Tool(name="lo_sth_create_week", description=(
        "StrongTri 整週建課：先確認教練身分（R 自動切 C，PLAT-36）→ 全部 dry-run（任一堂有 blocking 整批不寫）→ "
        "write=true 時逐堂三件套真寫（CONFIRM_SCHEDULE_CREATE）→ 讀回整週（readback_save_to 落檔）→ 新 classScheduleId 回寫 payload。"
        "預設 write=false＝只 dry-run。同日多堂的排序另呼叫 lo_sth_reorder_week。"),
        input_schema={"type": "object", "properties": {
            "payload_file": _PF, "write": {"type": "boolean", "default": False},
            "readback_save_to": {"type": "string", "description": "Mac 上的整週讀回輸出路徑"}},
            "required": ["payload_file"]}))
    tools.append(Tool(name="lo_sth_reorder_week", description=(
        "照 payload 順序排同日多堂（reorder_day 真寫）；教練既有課排前、新課照 payload 順序。撞 409001（裝置同步中）自動等 "
        "retry_wait_s 秒重試（PLAT-30）。先跑過 lo_sth_create_week write=true。"),
        input_schema={"type": "object", "properties": {
            "payload_file": _PF, "retry_wait_s": {"type": "integer", "default": 20},
            "retries": {"type": "integer", "default": 2}}, "required": ["payload_file"]}))

    async def _h_call(a): return await lo_sth_call(a["tool"], a.get("arguments"), a.get("arguments_file"), a.get("save_to"))
    async def _h_calc(a): return await lo_sth_calc_loads(a["payload_file"], bool(a.get("overwrite", False)))
    async def _h_create(a): return await lo_sth_create_week(a["payload_file"], bool(a.get("write", False)), a.get("readback_save_to"))
    async def _h_reorder(a): return await lo_sth_reorder_week(a["payload_file"], int(a.get("retry_wait_s", 20)), int(a.get("retries", 2)))

    handlers.update({"lo_sth_call": _h_call, "lo_sth_calc_loads": _h_calc,
                     "lo_sth_create_week": _h_create, "lo_sth_reorder_week": _h_reorder})
