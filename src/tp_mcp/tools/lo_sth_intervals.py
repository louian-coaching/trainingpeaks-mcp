"""FORK (羅教練 2026/10/02): StrongTri 逐段驗收（STH 版 lo_verify_intervals）。

STH 的 get_activity_detail 沒有 lap，但 `full=true` 會給逐秒時序（timeSeriesDataList：
time／heartRate／speed(km/h)／power／cadence…）。做法：
  1. 從計畫 classesJson 找出「工作段」（非 warmup／cooling／recover，且強度下緣 ≥ work_pct），
     相鄰工作段併成一個 block（例：漸進節奏跑三段＝1 block；2×15 分閾值＝2 block）。
  2. 時序做 20 秒平滑，騎車看功率、跑步看速度；高於門檻（最慢工作段下緣 ×0.9）的連續區間就是實際做的 block。
  3. 偵測到的 block 數＝計畫數時，block 內再依計畫段長（距離段按距離、時間段按時間）切出各小段。
每個 block 回：起點、時長、距離、平均功率／配速、平均／最高心率、前後半差；跑步另附每公里分段。
純函式、不連網，lo_sth_verify_intervals 與 lo_sth_prep_week 共用。
"""

from __future__ import annotations

from typing import Any

REST_MODES = ("warmup", "cooling", "recover")


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _pace(sec_per_km: float | None) -> str | None:
    if not sec_per_km or sec_per_km <= 0 or sec_per_km > 1800:
        return None
    s = int(round(sec_per_km))
    return f"{s // 60}:{s % 60:02d}"


def series(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """從 get_activity_detail(full=true) 回傳抓逐秒時序。"""
    def find(o: Any) -> Any:
        if isinstance(o, dict):
            if isinstance(o.get("timeSeriesDataList"), list):
                return o["timeSeriesDataList"]
            for v in o.values():
                r = find(v)
                if r is not None:
                    return r
        return None
    return find(detail) or []


def _smooth(xs: list[float], w: int = 20) -> list[float]:
    n, half = len(xs), w // 2
    pre = [0.0]
    for x in xs:
        pre.append(pre[-1] + x)
    out = []
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out.append((pre[b] - pre[a]) / (b - a))
    return out


def _plan_secs(s: dict[str, Any]) -> float:
    """計畫段的預估秒數（距離段＝距離×配速中值）。"""
    if s.get("capacity") == "distance":
        num = s.get("thresholdSpeedRangeNum") or [0, 0]
        mid = (_f(num[0]) + _f(num[1])) / 2 if num and num[0] else 0
        return _f(s.get("targetDistance")) * mid if mid else _f(s.get("targetSeconds"))
    return _f(s.get("targetSeconds"))


def work_blocks(cj: dict[str, Any], work_pct: float = 85.0, min_work_s: int = 60) -> list[list[dict[str, Any]]]:
    """計畫裡的工作段，依執行順序展開 times，相鄰工作段併成 block。
    短於 min_work_s 的段（主組前短衝／激活）不算工作段。"""
    seq: list[dict[str, Any] | None] = []
    for st in cj.get("stages") or []:
        for _ in range(int(st.get("times") or 1)):
            for s in st.get("sections") or []:
                rng = s.get("thresholdFtpRange") or s.get("thresholdSpeedRange") or [0, 0]
                is_work = ((s.get("stageMode") not in REST_MODES) and _f(rng[0]) >= work_pct
                           and _plan_secs(s) >= min_work_s)
                if not is_work:
                    seq.append(None)
                    continue
                num = s.get("thresholdFtpRangeNum") or s.get("thresholdSpeedRangeNum") or [None, None]
                seq.append({"capacity": s.get("capacity"), "seconds": int(_plan_secs(s)),
                            "km": _f(s.get("targetDistance")) if s.get("capacity") == "distance" else None,
                            "pct": rng, "num": num, "power": bool(s.get("thresholdFtpRange"))})
    blocks, cur = [], []
    for x in seq:
        if x is None:
            if cur:
                blocks.append(cur)
            cur = []
        else:
            cur.append(x)
    if cur:
        blocks.append(cur)
    return blocks


def _stats(rows: list[dict[str, Any]], is_bike: bool) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {}
    km = sum(_f(r.get("speed")) for r in rows) / 3600.0
    hrs = [_f(r.get("heartRate")) for r in rows if _f(r.get("heartRate")) > 0]
    out: dict[str, Any] = {"sec": n, "km": round(km, 2)}
    if is_bike:
        out["avg_w"] = round(sum(_f(r.get("power")) for r in rows) / n)
    else:
        out["pace"] = _pace(n / km if km else None)
    if hrs:
        out["avg_hr"], out["max_hr"] = round(sum(hrs) / len(hrs)), int(max(hrs))
    return out


def analyze(ts: list[dict[str, Any]], cj: dict[str, Any], work_pct: float = 85.0,
            gap_s: int = 15, min_s: int = 40) -> dict[str, Any]:
    is_bike = cj.get("sportType") in ("CYCLE", "RIDE")
    blocks = work_blocks(cj, work_pct)
    out: dict[str, Any] = {"sport": "RIDE" if is_bike else "RUN", "planned_blocks": len(blocks), "points": len(ts)}
    if not ts:
        return {**out, "error": "沒有逐秒時序（get_activity_detail 要帶 full=true）"}
    metric = [_f(r.get("power")) if is_bike else _f(r.get("speed")) for r in ts]
    sm = _smooth(metric)
    # 門檻：最慢那個工作段的下緣 ×0.9（騎＝瓦；跑＝km/h，由配速慢端換算）
    lows = []
    for b in blocks:
        for s in b:
            n0 = s["num"][0]
            if not n0:
                continue
            lows.append(_f(n0) if s["power"] else 3600.0 / _f(n0))
    if not lows:
        return {**out, "error": "計畫裡找不到工作段（強度下緣 ≥ work_pct 的段）"}
    thr = min(lows) * 0.9
    out["threshold"] = round(thr) if is_bike else _pace(3600.0 / thr)
    segs, start, last_hi = [], None, None
    for i, v in enumerate(sm):
        if v >= thr:
            if start is None:
                start = i
            last_hi = i
        elif start is not None and last_hi is not None and i - last_hi > gap_s:
            segs.append((start, last_hi + 1))
            start = None
    if start is not None:
        segs.append((start, (last_hi or start) + 1))
    shortest = min(sum(x["seconds"] for x in b) for b in blocks) if blocks else 0
    segs = [s for s in segs if s[1] - s[0] >= max(min_s, 0.5 * shortest)]
    out["detected_blocks"] = len(segs)
    if len(segs) > len(blocks):  # 多抓到的（暖身偷跑、熱身末段）：留最長的 N 段、維持時間順序
        keep = sorted(sorted(segs, key=lambda t: t[1] - t[0], reverse=True)[:len(blocks)])
        out["dropped"] = [{"start_s": a, "sec": b - a} for a, b in segs if (a, b) not in keep]
        segs = keep
    out["match"] = len(segs) == len(blocks)
    res = []
    for k, (a, b) in enumerate(segs):
        rows = ts[a:b]
        mid = (b - a) // 2
        blk = {"n": k + 1, "start_s": a, **_stats(rows, is_bike)}
        h1, h2 = _stats(rows[:mid], is_bike), _stats(rows[mid:], is_bike)
        if is_bike and h1 and h2:
            blk["halves_w"] = [h1["avg_w"], h2["avg_w"]]
        elif h1 and h2:
            blk["halves_pace"] = [h1.get("pace"), h2.get("pace")]
        if out["match"] and len(blocks[k]) > 1:  # block 內依計畫切小段
            plan = blocks[k]
            dist = bool(plan[0]["km"])
            cum, parts, pos = [], [], 0.0
            for r in rows:
                pos += _f(r.get("speed")) / 3600.0 if dist else 1.0
                cum.append(pos)
            tot_plan = sum((s["km"] or 0) if dist else s["seconds"] for s in plan) or 1
            scale = cum[-1] / tot_plan if cum else 1
            edge, i0 = 0.0, 0
            for s in plan:
                edge += ((s["km"] or 0) if dist else s["seconds"]) * scale
                i1 = next((j for j, c in enumerate(cum) if c >= edge), len(rows))
                parts.append({"plan_pct": s["pct"], **_stats(rows[i0:i1 + 1], is_bike)})
                i0 = i1 + 1
            blk["parts"] = parts
        res.append(blk)
    out["blocks"] = res
    if not is_bike:  # 每公里分段
        splits, pos, t0, nxt = [], 0.0, 0, 1.0
        for i, r in enumerate(ts):
            pos += _f(r.get("speed")) / 3600.0
            if pos >= nxt:
                splits.append({**_stats(ts[t0:i + 1], False), "km": int(nxt)})
                t0, nxt = i + 1, nxt + 1
        out["km_splits"] = [{"km": s["km"], "pace": s.get("pace"), "avg_hr": s.get("avg_hr")} for s in splits]
    return out
