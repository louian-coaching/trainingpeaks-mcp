"""羅教練 fork-only: ``lo_weekly_check`` — the whole 週檢 read in one call.

**Context (2026/10/03 週檢).** One weekly check meant, per athlete: resolve
identity, summarise the review week, read the PMC for TSB/CTL, summarise the
target week and the previous *full* week to compare loads. Five-plus calls per
athlete, each answer dragged through the model context. Two of that round's
mistakes came straight out of the plumbing, not the judgement:

* comparing the target week against a Mon–Sat review window instead of the
  previous full week (趙祎明 read +38% instead of +17%);
* comparing a TSS total that mixed strength/other into the tri load (Carrie
  read "coach-changed" when only a strength estimate differed).

This tool does the plumbing once, the same way for every athlete: the load
comparison is always target-week vs previous full week, tri TSS only, and
completion is judged by method/0 原則五 (via ``summarize_workouts``). The full
per-athlete payload goes to ``save_dir``; the response is one compact row per
athlete plus a text table, so twenty athletes fit in a screen.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any

from tp_mcp.client.context import athlete_override

logger = logging.getLogger(__name__)


def _shift(day: str, days: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=days)).isoformat()


def _pct(new: float, old: float) -> float | None:
    return round(100.0 * (new - old) / old, 1) if old else None


def _slug(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_") or "athlete"


def _name_matches(requested: str, resolved: str | None) -> bool:
    if not resolved:
        return False
    if requested.strip().isdigit():
        return True
    a, b = requested.strip().lower(), resolved.strip().lower()
    return a in b or b in a


def _session_counts(summary: dict[str, Any]) -> tuple[int, int]:
    """(done, scheduled) on training sessions — rest-day markers excluded."""
    from tp_mcp.tools.lo_review import _NOT_TRAINING

    done = total = 0
    for row in summary.get("workouts") or []:
        if (row.get("sport") or "") in _NOT_TRAINING:
            continue
        if not (row.get("planned") or row.get("tss_planned")):
            continue
        total += 1
        if row.get("type") == "completed":
            done += 1
    return done, total


_TRI = {"Swim", "Bike", "Run"}
_TRAIN = _TRI | {"Strength"}
# 週型（2026/11/17 週檢優化）：呼叫端從速查卡判好傳進來，燈號才認得分期。
WEEK_TYPES = {"normal": "", "off_season": "休賽", "post_race": "賽後", "race_week": "賽週",
              "data_gap": "缺資料"}


def _est_min(value: Any, unit: str | None) -> float:
    """Rough minutes, for WEIGHTING only (run 5.5 min/km, swim 2 min/100 m)."""
    if not isinstance(value, (int, float)):
        return 0.0
    return float(value) * {"min": 1.0, "km": 5.5, "m": 0.02}.get(unit or "", 0.0)


_EASY_WORDS = ("輕鬆", "恢復", "休息", "活動度", "伸展", "放鬆")


def _is_key(m: dict[str, Any]) -> bool:
    """Swim/bike/run session that is not an easy/recovery one (by title)."""
    return m.get("sport") in _TRI and not any(w in str(m.get("title") or "") for w in _EASY_WORDS)


def _day_gap(a: str, b: str) -> int:
    try:
        return abs((dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days)
    except ValueError:
        return 99


def _tag(m: dict[str, Any]) -> str:
    return f"{str(m.get('date') or '')[5:10]} {m.get('sport')} {m.get('title') or ''}".strip()


def _match_unplanned(
    missed: list[dict[str, Any]], unplanned: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]]:
    """Same sport on the same day — then on an adjacent day — = the planned session
    was done, just never paired (or moved by a day).

    2026/11/17: an athlete who rode 6 h read as "0/8 done, 4 added" because none of
    his files were paired to the plan.
    """
    left = list(unplanned)
    todo = list(missed)
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for gap in (0, 1):
        rest: list[dict[str, Any]] = []
        for m in todo:
            day = str(m.get("date") or "")[:10]
            hit = next((u for u in left if u.get("sport") == m.get("sport")
                        and _day_gap(str(u.get("date") or "")[:10], day) == gap), None)
            if hit is None:
                rest.append(m)
            else:
                left.remove(hit)
                matched.append((m, hit))
        todo = rest
    return todo, left, matched


def _volume_pct(review: dict[str, Any], matched: list[tuple[dict[str, Any], dict[str, Any]]]) -> int | None:
    """Tri completion by VOLUME (原則五), each session capped at 100% and weighted by
    its estimated planned minutes. Never TSS."""
    plan = done = 0.0
    today = str(review.get("today") or "")
    for w in review.get("workouts") or []:
        if w.get("sport") not in _TRI or not w.get("planned"):
            continue
        if today and w.get("type") != "completed" and str(w.get("date") or "")[:10] >= today:
            continue                      # today's / future sessions are not misses yet
        pm = _est_min(w.get("planned"), w.get("unit"))
        plan += pm
        if isinstance(w.get("done_pct"), (int, float)):
            done += pm * min(float(w["done_pct"]), 100.0) / 100.0
    for m, u in matched:
        if m.get("sport") in _TRI:
            done += min(_est_min(u.get("actual"), u.get("unit")), _est_min(m.get("planned"), m.get("unit")))
    return round(100.0 * done / plan) if plan else None


def _duplicates(review: dict[str, Any]) -> list[str]:
    """Same day, same sport, identical actual TSS = the same file uploaded twice
    (it double-counts ATL, so TSB reads lower than it is)."""
    seen: set[tuple[str, Any, float]] = set()
    dups: list[str] = []
    for w in review.get("workouts") or []:
        t = w.get("tss_actual")
        if w.get("type") != "completed" or not isinstance(t, (int, float)) or t <= 0:
            continue
        key = (str(w.get("date") or "")[:10], w.get("sport"), round(float(t), 2))
        if key in seen:
            dups.append(_tag(w))
        seen.add(key)
    return dups


def assess(
    review: dict[str, Any],
    prev: dict[str, Any],
    target: dict[str, Any],
    fitness: dict[str, Any] | None,
    *,
    ramp_warn_pct: float = 15.0,
    tsb_warn: float = -20.0,
    week_type: str = "normal",
) -> dict[str, Any]:
    """Pure: turn the four reads into one compact row with flags + level.

    ``flags`` drive the level; ``notes`` are information only. ``week_type`` keeps the
    lamp honest about periodisation: off_season = optional sessions (skipping them is
    not a miss), post_race / race_week = the load change is not a signal, data_gap =
    no device data, so nothing can be called missed.
    """
    wt = week_type if week_type in WEEK_TYPES else "normal"
    done, total = _session_counts(review)
    prev_tri = (prev.get("totals") or {}).get("tss_planned_tri", 0.0)
    tgt_tri = (target.get("totals") or {}).get("tss_planned_tri", 0.0)
    change = _pct(tgt_tri, prev_tri)
    missed, extra, matched = _match_unplanned(review.get("missed", []), review.get("unplanned", []))
    added_all = [u for u in extra if u.get("sport") in _TRAIN]
    added = [u for u in added_all if _est_min(u.get("actual"), u.get("unit")) >= 15]
    short = len(added_all) - len(added)          # warm-up / cool-down fragments
    other = [u for u in extra if u.get("sport") not in _TRAIN]
    dups = _duplicates(review)
    row: dict[str, Any] = {
        "week_type": wt,
        "review": {
            "done": done + len(matched), "scheduled": total,
            "volume_pct": _volume_pct(review, matched),
            "missed": [_tag(m) for m in missed],
            "matched_unplanned": [
                f"{_tag(m)} ← {u.get('title') or u.get('sport')} {u.get('actual')}{u.get('unit') or ''}"
                for m, u in matched],
            "incomplete": [f"{m['date'][5:]} {m['sport']} {m['title']} {m['done_pct']}%"
                           for m in review.get("incomplete", [])],
            "unplanned": [_tag(u) for u in added],
            "other_activities": len(other),
            "duplicates": dups,
            "pending_today": [f"{m['sport']} {m['title']}" for m in review.get("pending_today", [])],
            "tss_actual_tri": (review.get("totals") or {}).get("tss_actual_tri"),
        },
        "target_so_far": {
            "done": [_tag(w) + (f" {w['done_pct']:.0f}%" if isinstance(w.get("done_pct"), (int, float)) else "")
                     for w in target.get("workouts") or []
                     if w.get("type") == "completed" and w.get("sport") in _TRAIN],
            "missed": [_tag(m) for m in target.get("missed", [])],
            "pending_today": [f"{m['sport']} {m['title']}" for m in target.get("pending_today", [])],
        },
        "load": {
            "prev_week_planned_tri": prev_tri,
            "target_planned_tri": tgt_tri,
            "change_pct": change,
            "target_planned_other": (target.get("totals") or {}).get("tss_planned_other", 0.0),
            "target_sessions": _session_counts(target)[1],
        },
    }
    flags: list[str] = []
    notes: list[str] = []
    red = False
    tsb: Any = None
    if fitness:
        daily = fitness.get("daily_data") or []
        cur = fitness.get("current") or {}
        f = {"ctl": cur.get("ctl"), "atl": cur.get("atl"), "tsb": cur.get("tsb")}
        if len(daily) >= 2 and isinstance(daily[0].get("ctl"), (int, float)) and isinstance(f["ctl"], (int, float)):
            f["ctl_change_7d"] = round(f["ctl"] - daily[0]["ctl"], 1)
        row["fitness"] = f
        tsb = f.get("tsb")
    tired = isinstance(tsb, (int, float)) and tsb < tsb_warn
    if tired:
        flags.append(f"TSB {tsb}")
        red = red or tsb < tsb_warn - 10
    vol = row["review"]["volume_pct"]
    n_missed = len(missed)
    key_missed = [m for m in missed if _is_key(m)]
    incomplete = review.get("incomplete", [])
    key_incomplete = [m for m in incomplete if _is_key(m)]
    if wt == "data_gap":
        flags.append("資料缺口")
    elif wt == "off_season":
        if n_missed:
            notes.append(f"可選課未做{n_missed}堂")
    else:
        # Red = the skill's own thresholds: tri volume under 70%, or two key sessions
        # (not easy runs / strength) missed. A missed easy run is yellow, not red.
        if n_missed:
            flags.append(f"漏課{n_missed}堂")
            if key_missed:
                notes.append(f"其中關鍵課{len(key_missed)}堂")
        # Race / post-race weeks are light by design and the race is not a planned
        # session, so the 70% line only applies to a normal week.
        red = red or len(key_missed) >= 2 or (wt == "normal" and vol is not None and vol < 70)
        if key_incomplete:
            flags.append(f"未完成{len(key_incomplete)}堂")
        if len(incomplete) > len(key_incomplete):
            notes.append(f"輕鬆／肌力課未滿{len(incomplete) - len(key_incomplete)}堂")
    if added:
        flags.append(f"自加{len(added)}堂")
    if dups:
        flags.append(f"疑似重複檔{len(dups)}筆")
    if matched:
        notes.append(f"未配對或挪日{len(matched)}堂")
    if short:
        notes.append(f"短活動{short}筆")
    if other:
        notes.append(f"其他活動{len(other)}筆")
    if change is not None and abs(change) > ramp_warn_pct:
        # A bigger week is a signal only when the last one was not absorbed (volume
        # under 85%) or the athlete is already tired; a smaller week never is.
        strained = (vol is not None and vol < 85) or tired
        if wt == "normal" and change > 0 and strained:
            flags.append(f"下週tri {change:+.0f}%")
            red = red or change > 2 * ramp_warn_pct
        else:
            notes.append(f"下週tri {change:+.0f}%")
    if not tgt_tri:
        flags.append("下週未排課")
    row["flags"] = flags
    row["notes"] = notes
    row["level"] = "red" if red else ("yellow" if flags else "green")
    return row


async def _one(
    requested: str, *, review_start: str, review_end: str, prev_start: str, prev_end: str,
    target_start: str, target_end: str, today: str, incomplete_below: float,
    ramp_warn_pct: float, tsb_warn: float, save_dir: Path | None, week_type: str = "normal",
) -> dict[str, Any]:
    from tp_mcp.tools.fitness import tp_get_fitness
    from tp_mcp.tools.lo_review import athlete_identity, summarize_workouts
    from tp_mcp.tools.workouts import tp_get_workouts

    token = athlete_override.set(requested)
    try:
        ident = await athlete_identity()
        if not ident.get("athlete_name") and not ident.get("athlete_id"):
            return {"athlete": requested, "level": "error", "flags": ["身分解析失敗（名字打錯或重名？）"]}

        async def summary(start: str, end: str) -> dict[str, Any]:
            res = await tp_get_workouts(start_date=start, end_date=end, workout_filter="all")
            if res.get("isError"):
                raise RuntimeError(f"{start}~{end}: {res.get('error_code')} {res.get('message')}")
            return summarize_workouts(res.get("workouts") or [], incomplete_below=incomplete_below,
                                      today=today)

        review = await summary(review_start, review_end)
        same_window = (prev_start, prev_end) == (review_start, review_end)
        prev = review if same_window else await summary(prev_start, prev_end)
        target = await summary(target_start, target_end)
        fit = await tp_get_fitness(start_date=_shift(today, -7), end_date=today)
        fitness = None if (not isinstance(fit, dict) or fit.get("isError")) else fit
    except Exception as e:  # one athlete failing never sinks the batch
        logger.exception("weekly check failed for %s", requested)
        return {"athlete": requested, "level": "error", "flags": [f"讀取失敗：{e}"]}
    finally:
        athlete_override.reset(token)

    # 11/17: one batch right after a restart died with a bare 'internal error' — whatever goes
    # wrong past this point now comes back as that athlete's error row, with the reason.
    try:
        row = {"athlete": requested, **ident,
               **assess(review, prev, target, fitness, ramp_warn_pct=ramp_warn_pct, tsb_warn=tsb_warn,
                        week_type=week_type)}
        if not _name_matches(requested, ident.get("athlete_name")):
            row["flags"].insert(0, f"身分待確認：解析到 {ident.get('athlete_name')}")
            row["level"] = "red"
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
            p = save_dir / f"{_slug(str(ident.get('athlete_name') or requested))}.json"
            p.write_text(json.dumps({"athlete": requested, **ident, "today": today,
                                     "windows": {"review": [review_start, review_end],
                                                 "prev_full_week": [prev_start, prev_end],
                                                 "target": [target_start, target_end]},
                                     "review": review, "prev_full_week": prev, "target": target,
                                     "fitness": fitness, "check": row},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
            row["saved_to"] = str(p)
    except Exception as e:  # noqa: BLE001
        logger.exception("weekly check assess/save failed for %s", requested)
        return {"athlete": requested, **ident, "level": "error",
                "flags": [f"判讀或存檔失敗：{type(e).__name__}: {e}"]}
    return row


def _table(rows: list[dict[str, Any]]) -> str:
    mark = {"red": "R", "yellow": "Y", "green": "G", "error": "!"}
    lines = ["燈|選手|週型|完成|量%|TSB|CTLΔ7d|上週→下週 tri|旗標（括號＝僅供參考）|目標段已做/漏"]
    for r in rows:
        rv, ld, ft = r.get("review") or {}, r.get("load") or {}, r.get("fitness") or {}
        done = f"{rv.get('done')}/{rv.get('scheduled')}" if rv else "-"
        load = (f"{ld.get('prev_week_planned_tri'):g}→{ld.get('target_planned_tri'):g}"
                + (f" ({ld['change_pct']:+.0f}%)" if ld.get("change_pct") is not None else "")) if ld else "-"
        ts = r.get("target_so_far") or {}
        notes = r.get("notes") or []
        flag_txt = "、".join(r.get("flags") or []) or "-"
        if notes:
            flag_txt += "（" + "、".join(notes) + "）"
        vol = rv.get("volume_pct") if rv else None
        lines.append("|".join([
            mark.get(r.get("level"), "?"), str(r.get("athlete_name") or r.get("athlete")),
            WEEK_TYPES.get(r.get("week_type") or "normal", "") or "-", done,
            "-" if vol is None else str(vol),
            str(ft.get("tsb", "-")), str(ft.get("ctl_change_7d", "-")), load, flag_txt,
            f"{len(ts.get('done') or [])}/{len(ts.get('missed') or [])}" if ts else "-",
        ]))
    return "\n".join(lines)


async def lo_weekly_check(
    athletes: list[str],
    review_start: str,
    review_end: str,
    target_start: str,
    target_end: str,
    prev_start: str | None = None,
    prev_end: str | None = None,
    save_dir: str | None = None,
    today: str | None = None,
    incomplete_below: float = 90.0,
    ramp_warn_pct: float = 15.0,
    tsb_warn: float = -20.0,
    concurrency: int = 3,
    week_types: dict[str, str] | None = None,
    detail: str = "auto",
) -> dict[str, Any]:
    if not athletes:
        return {"isError": True, "error_code": "INVALID_ARGS", "message": "athletes is empty"}
    try:
        for d in (review_start, review_end, target_start, target_end):
            dt.date.fromisoformat(d)
    except ValueError as e:
        return {"isError": True, "error_code": "INVALID_ARGS", "message": str(e)}
    prev_start = prev_start or _shift(target_start, -7)
    prev_end = prev_end or _shift(target_end, -7)
    today = today or dt.date.today().isoformat()
    sd = Path(save_dir).expanduser() if save_dir else None
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    wts = {str(k).strip(): str(v) for k, v in (week_types or {}).items()}
    bad = sorted({v for v in wts.values() if v not in WEEK_TYPES})
    if bad:
        return {"isError": True, "error_code": "INVALID_ARGS",
                "message": f"week_types 只能是 {sorted(WEEK_TYPES)}，收到 {bad}"}

    async def run(name: str) -> dict[str, Any]:
        async with sem:
            return await _one(str(name).strip(), review_start=review_start, review_end=review_end,
                              prev_start=prev_start, prev_end=prev_end, target_start=target_start,
                              target_end=target_end, today=today, incomplete_below=incomplete_below,
                              ramp_warn_pct=ramp_warn_pct, tsb_warn=tsb_warn, save_dir=sd,
                              week_type=wts.get(str(name).strip(), "normal"))

    rows = await asyncio.gather(*(run(a) for a in athletes))
    order = {"error": 0, "red": 1, "yellow": 2, "green": 3}
    rows = sorted(rows, key=lambda r: order.get(r.get("level"), 9))
    out: dict[str, Any] = {
        "windows": {"review": [review_start, review_end], "prev_full_week": [prev_start, prev_end],
                    "target": [target_start, target_end], "today": today},
        "count": len(rows),
        "levels": {k: sum(1 for r in rows if r.get("level") == k) for k in ("red", "yellow", "green", "error")},
        "table": _table(rows),
        "athletes": rows,
        "note": ("完成度依 method/0 原則五（游泳公尺、騎車時間、跑步距離），TSS 只比量級。"
                 "負荷比較固定為「下週 vs 上一個完整週」、只計 tri（游騎跑），肌力/其他另列。"
                 "燈號只是分流，裁決仍由教練做。"),
    }
    # 2026/11/17: 35 athletes' full rows were ~16k tokens of context on top of the files
    # already written to save_dir. With save_dir the table is the response.
    mode = detail if detail in ("table", "rows") else ("table" if sd else "rows")
    if mode == "table":
        out.pop("athletes")
        out["errors"] = [r for r in rows if r.get("level") == "error"]
        out["detail"] = ("table — 每人完整列在 save_dir/<姓名>.json 的 check 欄"
                         if sd else "table — 沒給 save_dir，整列未落檔；要整列回傳請傳 detail='rows'")
        if sd:
            out["save_dir"] = str(sd)
    return out


async def lo_sweep_readbacks(athletes: list[str], start_date: str, end_date: str, save_dir: str,
                             concurrency: int = 3) -> dict[str, Any]:
    """FORK 2026-11-06 (coach menu): read a planned window back for many athletes at once,
    each in validate_week.py shape, plus ``manifest.json`` (athlete → file, TP id, name).

    Built for the rule sweep (tp-ai-layer/tools/rule_sweep.py): after a new rule enters
    method/, one call reads every athlete's future weeks, the script runs validate_week
    on each and keeps only the new rule's hits. Read-only; the lo_diff_week snapshot is
    NOT refreshed (a sweep must not absorb coach edits — TECH-56).
    """
    from tp_mcp.tools.lo_review import athlete_identity
    from tp_mcp.tools.lo_tools import lo_get_week_for_validate

    if not athletes:
        return {"isError": True, "error_code": "INVALID_ARGS", "message": "athletes is empty"}
    try:
        dt.date.fromisoformat(start_date), dt.date.fromisoformat(end_date)
    except ValueError as e:
        return {"isError": True, "error_code": "INVALID_ARGS", "message": str(e)}
    sd = Path(save_dir).expanduser()
    sd.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(requested: str) -> dict[str, Any]:
        async with sem:
            token = athlete_override.set(requested)
            try:
                ident = await athlete_identity()
                if not ident.get("athlete_id"):
                    return {"athlete": requested, "error": "身分解析失敗"}
                f = sd / f"{ident['athlete_id']}.json"
                res = await lo_get_week_for_validate(start_date=start_date, end_date=end_date,
                                                     save_to=str(f), _snapshot=False)
                if res.get("isError"):
                    return {"athlete": requested, **ident, "error": f"{res.get('error_code')} {res.get('message')}"}
                row = {"athlete": requested, **ident, "file": str(f), "count": res.get("count"),
                       "detail_failures": res.get("detail_failures")}
                if not _name_matches(requested, ident.get("athlete_name")):
                    row["identity_warning"] = f"解析到 {ident.get('athlete_name')}"
                return row
            except Exception as e:  # noqa: BLE001 — one athlete never sinks the sweep
                return {"athlete": requested, "error": str(e)}
            finally:
                athlete_override.reset(token)

    rows = await asyncio.gather(*(one(str(a).strip()) for a in athletes))
    manifest = {"window": [start_date, end_date], "athletes": rows}
    (sd / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"success": True, "save_dir": str(sd), "window": [start_date, end_date],
            "ok": sum(1 for r in rows if not r.get("error")), "errors": [r for r in rows if r.get("error")],
            "identity_warnings": [r for r in rows if r.get("identity_warning")],
            "next": ("python3 tp-ai-layer/tools/rule_sweep.py <save_dir> --rules R108,… "
                     "（讀速查卡旗標、只列指定規則的命中）")}


def register_lo_weekly(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(Tool(
        name="lo_weekly_check",
        description=(
            "週檢 in one call for a list of athletes: identity, review-week completion "
            "(method/0 原則五 — swim metres, bike time, run distance; never TSS), missed / "
            "incomplete / unplanned sessions, TSB + 7-day CTL change, and the target week's "
            "planned tri TSS vs the PREVIOUS FULL WEEK (default target−7 days; strength/other "
            "listed separately). Same-day same-sport unpaired files count as done; tri completion "
            "by volume (`volume_pct`); identical-TSS duplicates flagged; sessions already done in "
            "the target week listed (`target_so_far`). Pass `week_types` so the lamp knows the "
            "phase. With save_dir the response is the table only (full rows in the per-athlete "
            "JSON). Read-only. Athletes are resolved by name or ID like `athlete`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "athletes": {"type": "array", "items": {"type": "string"},
                             "description": "Athlete names or IDs (coach roster)."},
                "review_start": {"type": "string", "description": "YYYY-MM-DD, review window start"},
                "review_end": {"type": "string", "description": "YYYY-MM-DD, review window end"},
                "target_start": {"type": "string", "description": "YYYY-MM-DD, week being adjusted"},
                "target_end": {"type": "string", "description": "YYYY-MM-DD"},
                "prev_start": {"type": "string", "description": "Default target_start−7d (full week)."},
                "prev_end": {"type": "string", "description": "Default target_end−7d."},
                "save_dir": {"type": "string", "description": (
                    "Directory ON THE MACHINE RUNNING THIS SERVER for per-athlete JSON.")},
                "today": {"type": "string", "description": "YYYY-MM-DD; default server local date."},
                "incomplete_below": {"type": "number", "default": 90},
                "ramp_warn_pct": {"type": "number", "default": 15,
                                  "description": "Flag |target vs prev tri TSS| above this %."},
                "tsb_warn": {"type": "number", "default": -20},
                "concurrency": {"type": "integer", "default": 3},
                "week_types": {"type": "object", "additionalProperties": {
                    "type": "string", "enum": sorted(WEEK_TYPES)}, "description": (
                    "Per athlete (key = the same string as in `athletes`), from the 速查卡: "
                    "off_season = 休賽／自由活動週 (optional sessions are not misses), post_race / "
                    "race_week = load change is not a signal, data_gap = watch not syncing "
                    "(nothing is called missed). Omitted = normal.")},
                "detail": {"type": "string", "enum": ["auto", "table", "rows"], "default": "auto",
                           "description": "auto = table only when save_dir is given, else full rows."},
            },
            "required": ["athletes", "review_start", "review_end", "target_start", "target_end"],
        },
    ))

    async def _h(args: dict[str, Any]) -> dict[str, Any]:
        keys = ("prev_start", "prev_end", "save_dir", "today")
        return await lo_weekly_check(
            athletes=list(args.get("athletes") or []),
            review_start=args["review_start"], review_end=args["review_end"],
            target_start=args["target_start"], target_end=args["target_end"],
            incomplete_below=float(args.get("incomplete_below", 90)),
            ramp_warn_pct=float(args.get("ramp_warn_pct", 15)),
            tsb_warn=float(args.get("tsb_warn", -20)),
            concurrency=int(args.get("concurrency", 3)),
            week_types=args.get("week_types") or None,
            detail=str(args.get("detail") or "auto"),
            **{k: args.get(k) for k in keys},
        )

    handlers["lo_weekly_check"] = _h

    tools.append(Tool(
        name="lo_sweep_readbacks",
        description=(
            "Rule sweep read: for many athletes, read the planned window back in validate_week.py "
            "shape (one JSON per TP id) plus manifest.json in save_dir. Read-only; does NOT refresh "
            "the lo_diff_week snapshot. Then run tp-ai-layer/tools/rule_sweep.py on save_dir with "
            "--rules to list only the new rule's hits across the roster."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "athletes": {"type": "array", "items": {"type": "string"},
                             "description": "Athlete names or IDs (coach roster)."},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "save_dir": {"type": "string", "description": "Directory ON THE MACHINE RUNNING THIS SERVER."},
                "concurrency": {"type": "integer", "default": 3},
            },
            "required": ["athletes", "start_date", "end_date", "save_dir"],
        },
    ))

    async def _hs(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_sweep_readbacks(athletes=list(args.get("athletes") or []),
                                        start_date=args["start_date"], end_date=args["end_date"],
                                        save_dir=args["save_dir"], concurrency=int(args.get("concurrency", 3)))

    handlers["lo_sweep_readbacks"] = _hs
