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
)


def _err(code: str, message: str) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message}


def _basis_for(w: dict[str, Any]) -> str:
    """Which measure decides completion for this workout."""
    sport = w.get("sport") or ""
    fixed = _BASIS_BY_SPORT.get(sport)
    if fixed:
        # A swim with no prescribed distance has nothing to compare metres
        # against; fall back rather than report a meaningless 0%.
        if fixed == "distance" and not w.get("distance_planned_km"):
            return "duration"
        return fixed
    if sport == "Run":
        return "distance" if w.get("distance_planned_km") else "duration"
    return "duration"


def _pair(w: dict[str, Any], basis: str) -> tuple[float | None, float | None, str]:
    """(planned, actual, unit) on the completion basis, in reader-friendly units."""
    if basis == "distance":
        pl, ac = w.get("distance_planned_km"), w.get("distance_actual_km")
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


def summarize_workouts(
    workouts: list[dict[str, Any]],
    incomplete_below: float = 90.0,
) -> dict[str, Any]:
    """Strip descriptions, add per-sport completion, flag the gaps.

    Split out from the tool so it is testable without touching the API.
    """
    rows: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    unplanned: list[dict[str, Any]] = []
    per_sport: dict[str, dict[str, float]] = {}
    tss_planned_total = tss_actual_total = 0.0

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
    return out


async def lo_get_workouts_summary(
    start_date: str,
    end_date: str,
    workout_filter: str = "all",
    incomplete_below: float = 90.0,
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

    out = summarize_workouts(res.get("workouts") or [], incomplete_below=incomplete_below)
    out["date_range"] = res.get("date_range") or {"start": start_date, "end": end_date}
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
        )

    handlers["lo_get_workouts_summary"] = _h_summary
