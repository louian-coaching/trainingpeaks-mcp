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


def assess(
    review: dict[str, Any],
    prev: dict[str, Any],
    target: dict[str, Any],
    fitness: dict[str, Any] | None,
    *,
    ramp_warn_pct: float = 15.0,
    tsb_warn: float = -20.0,
) -> dict[str, Any]:
    """Pure: turn the four reads into one compact row with flags + level."""
    done, total = _session_counts(review)
    prev_tri = (prev.get("totals") or {}).get("tss_planned_tri", 0.0)
    tgt_tri = (target.get("totals") or {}).get("tss_planned_tri", 0.0)
    change = _pct(tgt_tri, prev_tri)
    row: dict[str, Any] = {
        "review": {
            "done": done, "scheduled": total,
            "missed": [f"{m['date'][5:]} {m['sport']} {m['title']}" for m in review.get("missed", [])],
            "incomplete": [f"{m['date'][5:]} {m['sport']} {m['title']} {m['done_pct']}%"
                           for m in review.get("incomplete", [])],
            "unplanned": [f"{m['date'][5:]} {m['sport']} {m.get('title') or ''}".strip()
                          for m in review.get("unplanned", [])],
            "pending_today": [f"{m['sport']} {m['title']}" for m in review.get("pending_today", [])],
            "tss_actual_tri": (review.get("totals") or {}).get("tss_actual_tri"),
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
    red = False
    if fitness:
        daily = fitness.get("daily_data") or []
        cur = fitness.get("current") or {}
        f = {"ctl": cur.get("ctl"), "atl": cur.get("atl"), "tsb": cur.get("tsb")}
        if len(daily) >= 2 and isinstance(daily[0].get("ctl"), (int, float)) and isinstance(f["ctl"], (int, float)):
            f["ctl_change_7d"] = round(f["ctl"] - daily[0]["ctl"], 1)
        row["fitness"] = f
        tsb = f.get("tsb")
        if isinstance(tsb, (int, float)) and tsb < tsb_warn:
            flags.append(f"TSB {tsb}")
            red = red or tsb < tsb_warn - 10
    n_missed = len(row["review"]["missed"])
    if n_missed:
        flags.append(f"漏課{n_missed}堂")
        red = red or n_missed >= 2
    if row["review"]["incomplete"]:
        flags.append(f"未完成{len(row['review']['incomplete'])}堂")
    if row["review"]["unplanned"]:
        flags.append(f"自加{len(row['review']['unplanned'])}堂")
    if change is not None and abs(change) > ramp_warn_pct:
        flags.append(f"下週tri {change:+.0f}%")
        red = red or change > 2 * ramp_warn_pct
    if not tgt_tri:
        flags.append("下週未排課")
    row["flags"] = flags
    row["level"] = "red" if red else ("yellow" if flags else "green")
    return row


async def _one(
    requested: str, *, review_start: str, review_end: str, prev_start: str, prev_end: str,
    target_start: str, target_end: str, today: str, incomplete_below: float,
    ramp_warn_pct: float, tsb_warn: float, save_dir: Path | None,
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

    row = {"athlete": requested, **ident,
           **assess(review, prev, target, fitness, ramp_warn_pct=ramp_warn_pct, tsb_warn=tsb_warn)}
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
    return row


def _table(rows: list[dict[str, Any]]) -> str:
    mark = {"red": "R", "yellow": "Y", "green": "G", "error": "!"}
    lines = ["燈|選手|完成|TSB|CTLΔ7d|上週→下週 tri|旗標"]
    for r in rows:
        rv, ld, ft = r.get("review") or {}, r.get("load") or {}, r.get("fitness") or {}
        done = f"{rv.get('done')}/{rv.get('scheduled')}" if rv else "-"
        load = (f"{ld.get('prev_week_planned_tri'):g}→{ld.get('target_planned_tri'):g}"
                + (f" ({ld['change_pct']:+.0f}%)" if ld.get("change_pct") is not None else "")) if ld else "-"
        lines.append("|".join([
            mark.get(r.get("level"), "?"), str(r.get("athlete_name") or r.get("athlete")), done,
            str(ft.get("tsb", "-")), str(ft.get("ctl_change_7d", "-")), load,
            "、".join(r.get("flags") or []) or "-",
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

    async def run(name: str) -> dict[str, Any]:
        async with sem:
            return await _one(str(name).strip(), review_start=review_start, review_end=review_end,
                              prev_start=prev_start, prev_end=prev_end, target_start=target_start,
                              target_end=target_end, today=today, incomplete_below=incomplete_below,
                              ramp_warn_pct=ramp_warn_pct, tsb_warn=tsb_warn, save_dir=sd)

    rows = await asyncio.gather(*(run(a) for a in athletes))
    order = {"error": 0, "red": 1, "yellow": 2, "green": 3}
    rows = sorted(rows, key=lambda r: order.get(r.get("level"), 9))
    return {
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


def register_lo_weekly(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(Tool(
        name="lo_weekly_check",
        description=(
            "週檢 in one call for a list of athletes: identity, review-week completion "
            "(method/0 原則五 — swim metres, bike time, run distance; never TSS), missed / "
            "incomplete / unplanned sessions, TSB + 7-day CTL change, and the target week's "
            "planned tri TSS vs the PREVIOUS FULL WEEK (default target−7 days; strength/other "
            "listed separately). Returns one compact row per athlete with flags and a "
            "red/yellow/green level, plus a text table; full per-athlete JSON goes to "
            "save_dir. Read-only. Athletes are resolved by name or ID like `athlete`."
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
            **{k: args.get(k) for k in keys},
        )

    handlers["lo_weekly_check"] = _h
