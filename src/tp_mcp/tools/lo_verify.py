"""羅教練 fork-only: per-segment verification (``lo_verify_intervals``).

The problem this removes
------------------------
Coach prescribes ``2 x 25min @173~195W`` inside a 3.5h long ride. To review it
you need each segment's average separately — "did the second 25min hold the
same power as the first" is the whole point of the session (PER-24). Today the
only per-segment source is ``lapData``, which depends on the athlete pressing
the lap button. Several athletes never do: the head unit writes one lap with
``LapTrigger: SessionEnd`` and the segments become unrecoverable. Reminding
them in the workout description has failed three times (2026/08/11, 09/13,
09/19 — see tp-ai-layer/memory/cards/*.md "分圈問題").

But the data is already there. ``tp_analyze_workout`` returns a per-second
time series, and the *prescription* already carries the segment boundaries in
``structured_workout``. Laying one over the other reconstructs every segment
without the athlete touching the lap button. That is all this tool does.

Design rule (same as lo_tools.py): WRAP upstream, never edit it. We call
``tp_analyze_workout`` and ``tp_get_workout`` and do arithmetic on what they
return.

Alignment caveat
----------------
The time series is wall-clock; the prescription is planned duration. A pause,
a traffic light, or a long warm-up makes them drift apart, and a drifted
overlay silently reports the wrong numbers — worse than reporting nothing. So:

  * every response carries ``drift_pct`` (actual elapsed vs prescribed total);
  * ``align="scale"`` stretches the prescription onto the actual elapsed time
    when the athlete simply ran long/short uniformly;
  * ``align="elapsed"`` (default) does not scale, and the response warns when
    ``|drift| > 5%`` that segment boundaries are probably unreliable.

Distance-based run workouts align on the ``Distance`` channel instead of
``time``, because that is what their ``begin``/``end`` are measured in.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# Channels we report per segment, in output order. Key = time-series key.
_CHANNELS = ("Power", "HeartRate", "Cadence", "Speed")

_DRIFT_WARN = 0.05  # |actual/planned - 1| above this ⇒ boundaries unreliable


def _round_half_up(x: float) -> int:
    """Round .5 away from zero, not to even.

    ``round()`` is banker's rounding: ``round(202.5) == 202``. The prescription
    the athlete reads says 203W (270W x 75%), so reporting 202 here would put
    the verification one watt off the課表 for every band ending in .5. Small,
    but it is exactly the kind of mismatch that costs ten minutes of "is this a
    real difference" when reviewing.
    """
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _err(code: str, message: str) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message}


# ---------------------------------------------------------------------------
# Prescription → flat segment list
# ---------------------------------------------------------------------------


def flatten_structure(structure: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand a TP ``structure`` into consecutive segments with absolute bounds.

    Each block carries its own ``begin``/``end`` (cumulative seconds for a
    duration workout, cumulative metres for a distance one). We re-anchor the
    cursor at every block's ``begin`` rather than trusting our own running sum,
    so one odd step cannot shift everything after it.

    Repetition blocks are expanded: ``reps`` copies of their inner steps, laid
    end to end.

    Returns segments with ``name``/``start``/``end``/``target_min``/
    ``target_max``/``intensity_class``. Rest and warm-up steps are kept — the
    caller decides what to look at, and dropping them here would break the
    "segment N of the printed workout" mapping the coach reads from.
    """
    out: list[dict[str, Any]] = []
    for block in structure or []:
        if not isinstance(block, dict):
            continue
        cursor = float(block.get("begin") or 0)
        length = block.get("length") or {}
        reps = 1
        if length.get("unit") == "repetition":
            try:
                reps = max(1, int(length.get("value") or 1))
            except (TypeError, ValueError):
                reps = 1
        steps = [s for s in (block.get("steps") or []) if isinstance(s, dict)]
        for rep_i in range(reps):
            for st in steps:
                sl = st.get("length") or {}
                try:
                    val = float(sl.get("value") or 0)
                except (TypeError, ValueError):
                    val = 0.0
                if val <= 0:
                    continue
                targets = st.get("targets") or [{}]
                t0 = targets[0] if isinstance(targets[0], dict) else {}
                name = st.get("name") or st.get("intensityClass") or "step"
                if reps > 1:
                    name = f"{name} #{rep_i + 1}"
                out.append({
                    "name": name,
                    "start": cursor,
                    "end": cursor + val,
                    "target_min": t0.get("minValue"),
                    "target_max": t0.get("maxValue"),
                    "intensity_class": st.get("intensityClass"),
                })
                cursor += val
    return out


def _normalize_manual(segments: list[dict[str, Any]]) -> list[dict[str, Any]] | str:
    """Validate caller-supplied segments; return the list or an error string."""
    out = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            return f"segments[{i}] must be an object"
        if "start_s" not in seg or "end_s" not in seg:
            return f"segments[{i}] needs start_s and end_s"
        try:
            start, end = float(seg["start_s"]), float(seg["end_s"])
        except (TypeError, ValueError):
            return f"segments[{i}] start_s/end_s must be numbers"
        if end <= start:
            return f"segments[{i}] end_s must be greater than start_s"
        out.append({
            "name": seg.get("name") or f"segment {i + 1}",
            "start": start,
            "end": end,
            "target_min": seg.get("target_min"),
            "target_max": seg.get("target_max"),
            "intensity_class": None,
        })
    return out


# ---------------------------------------------------------------------------
# Time series → per-segment stats
# ---------------------------------------------------------------------------


def _axis_value(point: dict[str, Any], axis: str) -> float | None:
    """Position of a sample on the alignment axis (seconds, or metres)."""
    if axis == "time":
        val = point.get("time")
    else:
        val = point.get("Distance")
        if isinstance(val, (int, float)):
            val = val * 1000.0  # charts report km; structure uses metres
    return float(val) if isinstance(val, (int, float)) else None


def _mean(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 1) if vals else None


def _segment_stats(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Averages for one segment, plus the first-half/second-half split.

    The split is what PER-24 actually asks for: a 25-minute race-power block
    that fades 12W from first half to second half was not held, even when its
    overall average lands inside the prescribed range.
    """
    stats: dict[str, Any] = {"samples": len(points)}
    for ch in _CHANNELS:
        vals = [p[ch] for p in points if isinstance(p.get(ch), (int, float))]
        avg = _mean(vals)
        if avg is not None:
            stats[f"avg_{ch.lower()}"] = avg
    pw = [p["Power"] for p in points if isinstance(p.get("Power"), (int, float))]
    if len(pw) >= 4:
        mid = len(pw) // 2
        first, second = _mean(pw[:mid]), _mean(pw[mid:])
        if first is not None and second is not None:
            stats["first_half_power"] = first
            stats["second_half_power"] = second
            stats["half_split_w"] = round(second - first, 1)
    return stats


def _in_range_pct(points: list[dict[str, Any]], lo: float | None, hi: float | None,
                  ftp: float | None) -> float | None:
    """Share of samples whose power sits inside the prescribed %FTP window."""
    if lo is None or hi is None or not ftp:
        return None
    lo_w, hi_w = ftp * float(lo) / 100.0, ftp * float(hi) / 100.0
    vals = [p["Power"] for p in points if isinstance(p.get("Power"), (int, float))]
    if not vals:
        return None
    inside = sum(1 for v in vals if lo_w <= v <= hi_w)
    return round(100.0 * inside / len(vals), 1)


async def lo_verify_intervals(
    workout_id: str,
    segments: list[dict[str, Any]] | None = None,
    align: str = "elapsed",
    ftp: float | None = None,
    include_rest: bool = False,
) -> dict[str, Any]:
    """Reconstruct per-segment averages from the time series, without laps.

    Args:
        workout_id: completed workout to verify.
        segments: optional explicit ``[{name, start_s, end_s, target_min,
            target_max}]``. Omit to derive them from the workout's own
            ``structured_workout``.
        align: ``elapsed`` (default, no scaling) or ``scale`` (stretch the
            prescription onto actual elapsed time).
        ftp: FTP/threshold used for the in-range percentage. Omit to skip it.
        include_rest: also report warm-up/rest/cool-down segments.
    """
    from tp_mcp.tools.analyze import tp_analyze_workout
    from tp_mcp.tools.workouts import tp_get_workout

    if align not in ("elapsed", "scale"):
        return _err("INVALID_ARGS", "align must be 'elapsed' or 'scale'")

    analysis = await tp_analyze_workout(workout_id)
    if analysis.get("isError"):
        return analysis
    data_file = analysis.get("data_file")
    if not data_file:
        return _err("NO_TIMESERIES", "Analysis returned no data file for this workout.")
    try:
        with open(data_file, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        return _err("NO_TIMESERIES", f"Could not read analysis data: {e}")
    points = [p for p in (raw.get("data") or []) if isinstance(p, dict)]
    if not points:
        return _err(
            "NO_TIMESERIES",
            "This workout has no per-second data (manual entry, or no device file uploaded).",
        )

    axis = "time"
    planned_total: float | None = None
    if segments:
        norm = _normalize_manual(segments)
        if isinstance(norm, str):
            return _err("INVALID_ARGS", norm)
        segs = norm
        source = "manual"
    else:
        detail = await tp_get_workout(workout_id)
        if isinstance(detail, dict) and detail.get("isError"):
            return detail
        sw = (detail or {}).get("structured_workout") or {}
        structure = sw.get("structure") or []
        if not structure:
            return _err(
                "NO_STRUCTURE",
                "Workout has no structured_workout to derive segments from — "
                "pass `segments` explicitly.",
            )
        if sw.get("primaryLengthMetric") == "distance":
            axis = "distance"
        segs = flatten_structure(structure)
        source = "structured_workout"
        if segs:
            planned_total = segs[-1]["end"]

    if not segs:
        return _err("NO_STRUCTURE", "No usable segments found.")

    axis_vals = [v for v in (_axis_value(p, axis) for p in points) if v is not None]
    if not axis_vals:
        return _err(
            "NO_TIMESERIES",
            f"Time series has no '{'time' if axis == 'time' else 'Distance'}' channel to align on.",
        )
    actual_total = max(axis_vals)

    drift_pct = None
    if planned_total:
        drift_pct = round(100.0 * (actual_total / planned_total - 1.0), 1)
        if align == "scale":
            factor = actual_total / planned_total
            segs = [{**s, "start": s["start"] * factor, "end": s["end"] * factor} for s in segs]

    rows = []
    for seg in segs:
        if not include_rest and seg.get("intensity_class") in ("warmUp", "rest", "coolDown"):
            continue
        in_seg = [
            p for p in points
            if (v := _axis_value(p, axis)) is not None and seg["start"] <= v < seg["end"]
        ]
        row: dict[str, Any] = {
            "name": seg["name"],
            "start_s" if axis == "time" else "start_m": round(seg["start"], 1),
            "end_s" if axis == "time" else "end_m": round(seg["end"], 1),
        }
        if seg.get("target_min") is not None:
            row["target_pct"] = [seg["target_min"], seg["target_max"]]
            if ftp:
                row["target_w"] = [
                    _round_half_up(ftp * float(seg["target_min"]) / 100.0),
                    _round_half_up(ftp * float(seg["target_max"]) / 100.0),
                ]
        row.update(_segment_stats(in_seg))
        pct = _in_range_pct(in_seg, seg.get("target_min"), seg.get("target_max"), ftp)
        if pct is not None:
            row["in_range_pct"] = pct
        rows.append(row)

    out: dict[str, Any] = {
        "success": True,
        "workout_id": workout_id,
        "segment_source": source,
        "align": align,
        "axis": axis,
        "segments": rows,
        "time_series_points": len(points),
    }
    if drift_pct is not None:
        out["drift_pct"] = drift_pct
        if align == "elapsed" and abs(drift_pct) > _DRIFT_WARN * 100:
            out["warning"] = (
                f"Actual elapsed is {drift_pct:+.1f}% vs the prescription, so segment "
                f"boundaries drift by up to {abs(actual_total - planned_total):.0f}"
                f"{'s' if axis == 'time' else 'm'} by the end of the session. "
                f"Re-run with align='scale' if the athlete simply went long/short "
                f"uniformly; treat late segments as approximate otherwise."
            )
    lap_count = len(analysis.get("lapData") or [])
    if lap_count <= 1:
        out["note"] = (
            "Device recorded a single lap — these segments come from the prescription, "
            "not from lap buttons."
        )
    return out


def register_lo_verify(tools: list[Any], handlers: dict[str, Any]) -> None:
    """Append ``lo_verify_intervals`` to ``tools``/``handlers`` (server.py接點)."""
    from mcp.types import Tool  # local import: keep module importable in tests

    tools.append(Tool(
        name="lo_verify_intervals",
        description=(
            "Reconstruct per-segment averages (power/HR/cadence/speed, plus the "
            "first-half vs second-half power split) for a completed workout WITHOUT "
            "needing device laps. Segments come from the workout's own "
            "structured_workout by default, so an athlete who never presses the lap "
            "button can still be verified — this is the tool for 'did the second "
            "25-minute race-power block hold the same watts as the first' (PER-24). "
            "Reports drift between prescribed and actual elapsed time; pass "
            "align='scale' when the session ran uniformly long or short, or supply "
            "`segments` explicitly to check arbitrary windows."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workout_id": {"type": "string", "description": "Completed workout ID."},
                "segments": {
                    "type": "array",
                    "description": (
                        "Optional explicit segments [{name, start_s, end_s, target_min, "
                        "target_max}]. Omit to derive them from structured_workout."
                    ),
                    "items": {"type": "object"},
                },
                "align": {
                    "type": "string",
                    "enum": ["elapsed", "scale"],
                    "default": "elapsed",
                    "description": (
                        "elapsed = use prescribed boundaries as-is; scale = stretch them "
                        "onto actual elapsed time."
                    ),
                },
                "ftp": {
                    "type": "number",
                    "description": "FTP/threshold for target watts and in_range_pct. Optional.",
                },
                "include_rest": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also report warm-up / rest / cool-down segments.",
                },
            },
            "required": ["workout_id"],
        },
    ))

    async def _h_verify(args: dict[str, Any]) -> dict[str, Any]:
        # `athlete` is popped into the athlete_override contextvar by call_tool
        # before we get here, so both upstream calls already target the right
        # athlete without us passing anything.
        a = dict(args)
        return await lo_verify_intervals(
            workout_id=a["workout_id"],
            segments=a.get("segments"),
            align=a.get("align", "elapsed"),
            ftp=a.get("ftp"),
            include_rest=bool(a.get("include_rest", False)),
        )

    handlers["lo_verify_intervals"] = _h_verify
