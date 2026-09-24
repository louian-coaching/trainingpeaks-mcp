"""羅教練 fork-only: the pre-scheduling review read (``lo_get_workouts_summary``).

Two problems, one tool.

**Context.** ``tp_get_workouts`` returns every workout's full ``description``
— the workout body plus the whole coaching write-up. Reviewing one athlete's
week means pulling ~11 of those into the model context, every single round,
just to see what got done. The reviewer wants five columns, not five thousand
words. ``save_to`` is the wrong lever here: it drops the payload to disk and
returns a summary so thin it has no TSS in it, so you end up reading the file
back anyway.

**Correctness.** ``method/0`` 原則五 and ``method/5 §5.0 排課前置①`` are
explicit: completion is judged by the volume in the workout body — metres for
swim, time for bike, distance for run — and **never** by TSS. TSS moves with
execution intensity and device estimation error, so a session done in full at
slightly lower intensity reads as "incomplete" and a slow long swim reads as
over-done (the 2026/08/03 巴斯 incident: TSS said 74%, the athlete had
actually swum 103% of the prescribed metres). Yet TSS is the field sitting
right there in the list response, so it is the one that gets used.

So this tool computes ``done_pct`` from the *right* denominator per sport and
returns TSS only as planned/actual figures for load planning, never as a
ratio. Getting that wrong is not a style issue — it inverts the conclusion.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# method/5 §5.0 排課前置① — what "did they complete it" is measured in.
# Run is resolved per workout: distance when the prescription carries one,
# otherwise time (輕鬆跑 and other time-based runs).
_BASIS_BY_SPORT = {
    "Swim": "distance",
    "Bike": "duration",
    "MtnBike": "duration",
    "Strength": "duration",
}

_NOTE = (
    "done_pct 依 method/0 原則五＋method/5 §5.0 排課前置①：游泳看公尺、騎車看時間、"
    "跑步看距離（時間制課看時間）、肌力看時間。TSS 欄位只供量級規劃與 CTL 走勢，"
    "不得用來判斷完成度或訓練品質。"
    "｜planned_source=body 表示 TP 的 distancePlanned 是空的、計畫距離由課表本體加總而來；"
    "本體裡的時間制段落（如長跑收尾的緩跑5分鐘）不計入，故該堂 done_pct 會略為偏高。"
)


# --------------------------------------------------------------------------
# Planned distance from the workout body
# --------------------------------------------------------------------------
# TrainingPeaks' `distancePlanned` field is empty on every workout this fork
# creates: the prescription travels as a structured workout and as the body
# text, and neither writes that field. So the read that is supposed to judge
# swims by metres and runs by kilometres had nothing to compare against and
# quietly fell back to time for every single workout — the exact substitution
# 原則五 forbids, just one step removed from TSS.
#
# The numbers are in the body, in the same notation the coach's own validators
# already parse (validate_week.swim_row and its `N趟／N組` multiplier):
#
#     - 500公尺@80-85%, 2趟, 間休30秒      → 1000 m
#     - 25公尺加速游+25公尺放鬆, 2組        →  100 m   (a rep split into pieces)
#     - 8公里@6:35~6:20/km                 → 8000 m
#
# Everything after 「－－」 is the coaching write-up, which is full of numbers
# that are not volume (每100公尺1:52), so it is cut off first.
_BODY_SPLIT = "－－"
_REPS_RX = re.compile(r"[,，]\s*(\d+)\s*[趟組]")
_METRE_RX = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*公尺")
_KM_RX = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*公里")


def planned_distance_m(description: str | None) -> float | None:
    """Prescribed distance in metres, summed from the workout body.

    Returns None when the body prescribes no distance at all (a time-based
    easy run, a bike session) — the caller then judges on time, which is
    correct for those.

    Time-based segments inside a distance session (「緩跑5分鐘」at the end of a
    long run) contribute nothing, so the figure is the distance the *prescribed
    distance segments* add up to, not the odometer reading the athlete will
    finish on. done_pct therefore reads a little high on runs that close with a
    timed cool-down; it is the main set that the review is judging.
    """
    if not description:
        return None
    body, _, _ = description.partition(_BODY_SPLIT)
    # 「左右各25公尺」= 25 m each side = 50 m (unilateral drills, SWIM-xx house style)
    body = re.sub(r"左右各\s*(\d+(?:\.\d+)?)\s*公尺", lambda m: f"{float(m.group(1)) * 2:g}公尺", body)
    total = 0.0
    for line in body.splitlines():
        line = line.strip().lstrip("-－ 　")
        if not line:
            continue
        metres = sum(float(x) for x in _METRE_RX.findall(line))
        metres += sum(float(x) * 1000 for x in _KM_RX.findall(line))
        if not metres:
            continue
        reps = _REPS_RX.search(line)
        total += metres * (int(reps.group(1)) if reps else 1)
    return round(total, 1) if total else None


def _err(code: str, message: str) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message}


def _planned_km(w: dict[str, Any]) -> tuple[float | None, str]:
    """(planned km, where it came from) — TP's own field first, body second."""
    tp_km = w.get("distance_planned_km")
    if isinstance(tp_km, (int, float)) and tp_km:
        return float(tp_km), "tp"
    metres = planned_distance_m(w.get("description"))
    return (metres / 1000.0, "body") if metres else (None, "none")


def _basis_for(w: dict[str, Any]) -> str:
    """Which measure decides completion for this workout."""
    sport = w.get("sport") or ""
    fixed = _BASIS_BY_SPORT.get(sport)
    if fixed:
        # A swim whose body prescribes no metres either has nothing to compare
        # against; fall back rather than report a meaningless 0%.
        if fixed == "distance" and _planned_km(w)[0] is None:
            return "duration"
        return fixed
    if sport == "Run":
        return "distance" if _planned_km(w)[0] is not None else "duration"
    return "duration"


def _pair(w: dict[str, Any], basis: str) -> tuple[float | None, float | None, str]:
    """(planned, actual, unit) on the completion basis, in reader-friendly units."""
    if basis == "distance":
        pl, ac = _planned_km(w)[0], w.get("distance_actual_km")
        if (w.get("sport") or "") == "Swim":
            return (
                round(pl * 1000) if isinstance(pl, (int, float)) else None,
                round(ac * 1000) if isinstance(ac, (int, float)) else None,
                "m",
            )
        return (
            round(pl, 2) if isinstance(pl, (int, float)) else None,
            round(ac, 2) if isinstance(ac, (int, float)) else None,
            "km",
        )
    pl, ac = w.get("duration_planned"), w.get("duration_actual")  # hours
    return (
        round(pl * 60, 1) if isinstance(pl, (int, float)) else None,
        round(ac * 60, 1) if isinstance(ac, (int, float)) else None,
        "min",
    )


def _num(val: Any) -> float:
    return float(val) if isinstance(val, (int, float)) else 0.0


def _merge_fragments(workouts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """FORK: fold a device-split recording into the planned session it belongs to.

    A watch that stops mid-session uploads two files; TP pairs one with the
    planned workout and leaves the other as an untitled, unplanned activity on
    the same day and sport (09/23: swim class 24.8 min + 11.6 min). Reported
    raw, that is one false "incomplete 41%" plus one false "unplanned". A
    completed row with no plan and no title is added to the same-day, same-sport
    completed row that has a plan; nothing else is touched."""
    def has_plan(w: dict[str, Any]) -> bool:
        return bool(w.get("duration_planned") or w.get("distance_planned_km") or w.get("tss_planned")
                    or planned_distance_m(w.get("description")))

    out = [dict(w) for w in workouts]
    merged: list[dict[str, Any]] = []
    drop: set[int] = set()
    for i, frag in enumerate(out):
        if frag.get("type") != "completed" or has_plan(frag) or (frag.get("title") or "").strip():
            continue
        host = next((h for j, h in enumerate(out) if j != i and j not in drop
                     and h.get("type") == "completed" and has_plan(h)
                     and str(h.get("date"))[:10] == str(frag.get("date"))[:10]
                     and h.get("sport") == frag.get("sport")), None)
        if host is None:
            continue
        for key in ("duration_actual", "distance_actual_km", "tss_actual"):
            if isinstance(frag.get(key), (int, float)):
                host[key] = (host.get(key) or 0) + frag[key]
        host.setdefault("merged_fragments", []).append(frag.get("id"))
        merged.append({"fragment_id": frag.get("id"), "into": host.get("id"), "date": str(frag.get("date"))[:10],
                       "sport": frag.get("sport")})
        drop.add(i)
    return [w for i, w in enumerate(out) if i not in drop], merged


def summarize_workouts(
    workouts: list[dict[str, Any]],
    incomplete_below: float = 90.0,
    merge_fragments: bool = True,
) -> dict[str, Any]:
    """Strip descriptions, add per-sport completion, flag the gaps.

    Split out from the tool so it is testable without touching the API.
    """
    rows: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    unplanned: list[dict[str, Any]] = []
    per_sport: dict[str, dict[str, float]] = {}
    tss_planned_total = tss_actual_total = 0.0
    merged: list[dict[str, Any]] = []
    if merge_fragments:
        workouts, merged = _merge_fragments(workouts)

    for w in workouts:
        basis = _basis_for(w)
        planned, actual, unit = _pair(w, basis)
        completed = (w.get("type") == "completed")
        row: dict[str, Any] = {
            "id": w.get("id"),
            "date": w.get("date"),
            "sport": w.get("sport"),
            "title": w.get("title"),
            "type": w.get("type"),
            "basis": basis,
            "planned_source": _planned_km(w)[1] if basis == "distance" else "tp",
            "planned": planned,
            "actual": actual,
            "unit": unit,
        }
        if planned and actual is not None:
            row["done_pct"] = round(100.0 * actual / planned, 1)
        # TSS travels as raw figures only — never as a ratio (原則五).
        for key in ("tss_planned", "tss_actual"):
            if w.get(key) is not None:
                row[key] = w[key]
        if w.get("merged_fragments"):
            row["merged_fragments"] = w["merged_fragments"]
        rows.append(row)

        tss_planned_total += _num(w.get("tss_planned"))
        tss_actual_total += _num(w.get("tss_actual"))

        bucket = per_sport.setdefault(
            w.get("sport") or "?", {f"planned_{unit}": 0.0, f"actual_{unit}": 0.0}
        )
        bucket[f"planned_{unit}"] = round(bucket.get(f"planned_{unit}", 0.0) + _num(planned), 2)
        bucket[f"actual_{unit}"] = round(bucket.get(f"actual_{unit}", 0.0) + _num(actual), 2)

        if completed and not planned:
            unplanned.append({k: row[k] for k in ("date", "sport", "title", "actual", "unit")})
        elif completed and row.get("done_pct") is not None and row["done_pct"] < incomplete_below:
            incomplete.append({
                "date": row["date"], "sport": row["sport"], "title": row["title"],
                "planned": planned, "actual": actual, "unit": unit,
                "done_pct": row["done_pct"],
            })

    totals: dict[str, Any] = {
        "tss_planned": round(tss_planned_total, 1),
        "tss_actual": round(tss_actual_total, 1),
        "per_sport": per_sport,
    }
    out: dict[str, Any] = {"workouts": rows, "count": len(rows), "totals": totals, "note": _NOTE}
    if incomplete:
        out["incomplete"] = incomplete
    if unplanned:
        out["unplanned"] = unplanned
    if merged:
        out["merged_fragments"] = merged
    return out


async def lo_get_workouts_summary(
    start_date: str,
    end_date: str,
    workout_filter: str = "all",
    incomplete_below: float = 90.0,
    save_to: str | None = None,
    merge_fragments: bool = True,
) -> dict[str, Any]:
    """List workouts without their descriptions, with per-sport completion.

    Args:
        start_date: YYYY-MM-DD.
        end_date: YYYY-MM-DD.
        workout_filter: all | planned | completed.
        incomplete_below: done_pct under this lands the workout in
            ``incomplete``. Default 90.
    """
    from tp_mcp.tools.workouts import tp_get_workouts

    if workout_filter not in ("all", "planned", "completed"):
        return _err("INVALID_ARGS", "type must be all, planned or completed")

    res = await tp_get_workouts(
        start_date=start_date, end_date=end_date, workout_filter=workout_filter
    )
    if res.get("isError"):
        return res

    out = summarize_workouts(res.get("workouts") or [], incomplete_below=incomplete_below,
                             merge_fragments=merge_fragments)
    out["date_range"] = res.get("date_range") or {"start": start_date, "end": end_date}
    if save_to:
        import json
        from pathlib import Path
        p = Path(save_to).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"saved_to": str(p), "count": out["count"], "totals": out["totals"],
                **{k: out[k] for k in ("incomplete", "unplanned", "merged_fragments") if k in out},
                "date_range": out["date_range"]}
    return out


def register_lo_review(tools: list[Any], handlers: dict[str, Any]) -> None:
    """Append ``lo_get_workouts_summary`` to ``tools``/``handlers``."""
    from mcp.types import Tool  # local import: keep module importable in tests

    tools.append(Tool(
        name="lo_get_workouts_summary",
        description=(
            "List workouts WITHOUT their descriptions, with completion measured the "
            "way method/0 原則五 requires: swim by metres, bike by time, run by "
            "distance (or time for time-based runs), strength by time — never by TSS. "
            "Use this for 排課前置① (reviewing last week before scheduling the next): "
            "it returns the five columns that decision needs instead of ~11 full "
            "coaching write-ups, and flags every session under `incomplete_below` "
            "plus any unplanned session the athlete added. TSS is returned as raw "
            "planned/actual figures for load planning only, never as a ratio."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "type": {
                    "type": "string",
                    "enum": ["all", "planned", "completed"],
                    "default": "all",
                    "description": "Filter by status.",
                },
                "save_to": {"type": "string", "description": (
                    "Absolute path ON THE MACHINE RUNNING THIS SERVER: write the full summary there and "
                    "return only totals + incomplete/unplanned (keeps a multi-week review out of context).")},
                "merge_fragments": {"type": "boolean", "default": True, "description": (
                    "Fold an untitled, unplanned completed activity into the same-day, same-sport planned "
                    "session (device-split recordings). Set false to see the raw rows.")},
                "incomplete_below": {
                    "type": "number",
                    "default": 90,
                    "description": (
                        "done_pct under this value lists the workout under `incomplete`."
                    ),
                },
            },
            "required": ["start_date", "end_date"],
        },
    ))

    async def _h_summary(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_get_workouts_summary(
            start_date=args["start_date"],
            end_date=args["end_date"],
            workout_filter=args.get("type", "all"),
            incomplete_below=float(args.get("incomplete_below", 90)),
            save_to=args.get("save_to"),
            merge_fragments=bool(args.get("merge_fragments", True)),
        )

    handlers["lo_get_workouts_summary"] = _h_summary
