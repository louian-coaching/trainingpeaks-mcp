"""羅教練 fork-only tools (``lo_`` prefix).

Design rule: this module WRAPS upstream tools, it never edits them. Every
``lo_*`` tool calls the upstream ``tp_*`` function, then reads the workout
back with ``tp_get_workout`` and compares field by field. "success" here
means *landed*, not *TP returned 200*.

Why this exists (tp-ai-layer/tools/PITFALLS.md, TECH-35 family):
  TECH-12  no-TSS Strength workout returns success but never lands
  TECH-16  update with only ``description`` silently flips sport Race→Run
  TECH-25  update with only ``sport`` does not take effect; Race lands as Other
  TECH-35  ``tss`` (typo for ``tss_planned``) silently dropped
  TECH-37  update carrying ``structured_workout`` + other fields lands nothing
  TECH-38  DayOff→Swim/Bike/Run in place via ``sport`` instead of delete+create

Registration: ``register_lo_tools(TOOLS, handlers)`` at the end of server.py
is the only touch point in upstream code, so upstream merges conflict on at
most two lines.
"""

from __future__ import annotations

import logging
from typing import Any

from tp_mcp.tools.workouts import SPORT_TYPE_MAP, tp_get_workout, tp_update_workout

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parameter aliases (evaluation item B#2)
# ---------------------------------------------------------------------------

#: alias -> canonical. Applied by ``normalize_aliases`` for EVERY tool whose
#: schema has the canonical key and not the alias. Keep this list boring:
#: only names that have actually been typed by mistake.
PARAM_ALIASES: dict[str, str] = {
    "tss": "tss_planned",
    "tssPlanned": "tss_planned",
    "duration": "duration_minutes",
    "durationMinutes": "duration_minutes",
    "distance": "distance_km",
    "distanceKm": "distance_km",
    "workoutId": "workout_id",
    "date_str": "date",
    "workout_day": "date",
    "desc": "description",
    "structure_raw": "structured_workout",
    "structuredWorkout": "structured_workout",
    "subtype": "subtype_id",
    "hidden": "is_hidden",
}


def normalize_aliases(properties: dict[str, Any] | None, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Rewrite aliased keys to their canonical name.

    Returns (new_args, notes). A key is rewritten only when the canonical key
    is in ``properties``, the alias itself is not, and the canonical key was
    not also supplied (in which case the alias is left alone and the global
    unknown-key check will reject it — two spellings of the same field is a
    real ambiguity, not a typo).
    """
    props = set(properties or {})
    out = dict(args)
    notes: list[str] = []
    for alias, canonical in PARAM_ALIASES.items():
        if alias in out and alias not in props and canonical in props and canonical not in out:
            out[canonical] = out.pop(alias)
            notes.append(f"{alias} -> {canonical}")
    return out, notes


# ---------------------------------------------------------------------------
# Read-back verification (evaluation item B#1)
# ---------------------------------------------------------------------------

_UPDATE_FIELDS = (
    "sport", "subtype_id", "title", "description", "date", "duration_minutes",
    "distance_km", "tss_planned", "tags", "athlete_comment", "coach_comment",
    "feeling", "rpe", "is_hidden", "structure", "structured_workout",
)

#: Fields tp_get_workout does not echo, so they can be sent but not verified.
_UNVERIFIABLE = {"subtype_id", "tags", "athlete_comment", "coach_comment", "is_hidden", "structure"}

#: These must go in their own PUT (TECH-37).
_STRUCTURE_KEYS = ("structured_workout", "structure")


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message, **extra}


def _num_close(a: Any, b: Any, tol: float) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def _structure_fingerprint(sw: Any) -> dict[str, Any] | None:
    """Compact, order-sensitive summary of a structured workout."""
    if not isinstance(sw, dict):
        return None
    steps = sw.get("structure") or []
    poly = sw.get("polyline") or []
    first_begin = last_end = None
    if isinstance(steps, list) and steps:
        if isinstance(steps[0], dict):
            first_begin = steps[0].get("begin")
        if isinstance(steps[-1], dict):
            last_end = steps[-1].get("end")
    return {
        "primaryLengthMetric": sw.get("primaryLengthMetric"),
        "primaryIntensityMetric": sw.get("primaryIntensityMetric"),
        "blocks": len(steps) if isinstance(steps, list) else 0,
        "begin": first_begin,
        "end": last_end,
        "polyline_points": len(poly) if isinstance(poly, list) else 0,
    }


def _landed_value(field: str, detail: dict[str, Any]) -> Any:
    metrics = detail.get("metrics") or {}
    if field == "duration_minutes":
        hours = metrics.get("duration_planned")
        return round(hours * 60.0, 2) if isinstance(hours, (int, float)) else None
    if field == "tss_planned":
        return metrics.get("tss_planned")
    if field == "distance_km":
        return metrics.get("distance_planned_km")
    if field == "date":
        d = detail.get("date")
        return d[:10] if isinstance(d, str) else d
    if field == "structured_workout":
        return _structure_fingerprint(detail.get("structured_workout"))
    return detail.get(field)


def _compare(field: str, sent: Any, landed: Any) -> bool:
    if field in ("duration_minutes",):
        return _num_close(sent, landed, 0.05)
    if field in ("tss_planned",):
        return _num_close(sent, landed, 0.5)
    if field in ("distance_km",):
        return _num_close(sent, landed, 0.01)
    if field == "date":
        return str(sent)[:10] == str(landed)[:10]
    if field == "structured_workout":
        return _structure_fingerprint(sent) == landed
    if field in ("feeling", "rpe"):
        return _num_close(sent, landed, 0)
    return sent == landed


def _verify(sent: dict[str, Any], detail: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Field-by-field comparison. Returns (report, mismatched_field_names)."""
    report: dict[str, Any] = {}
    bad: list[str] = []
    for field, value in sent.items():
        if field in _UNVERIFIABLE:
            report[field] = {"sent": value, "landed": None, "ok": None, "note": "not echoed by tp_get_workout"}
            continue
        landed = _landed_value(field, detail)
        ok = _compare(field, value, landed)
        shown_sent = _structure_fingerprint(value) if field == "structured_workout" else value
        report[field] = {"sent": shown_sent, "landed": landed, "ok": ok}
        if not ok:
            bad.append(field)
    return report, bad


async def _read(workout_id: str) -> dict[str, Any]:
    detail = await tp_get_workout(workout_id=str(workout_id))
    if isinstance(detail, dict) and detail.get("isError"):
        raise RuntimeError(f"read-back failed: {detail.get('message')}")
    return detail


async def lo_update_workout_verified(workout_id: str, **fields: Any) -> dict[str, Any]:
    """Update a workout, then read it back and compare every field.

    - Unknown/aliased keys: normalised by the server layer before we get here.
    - TECH-37: if ``structured_workout``/``structure`` is sent together with
      other fields, the update is split into two PUTs (other fields first,
      structure second), each read back.
    - TECH-16/25: ``sport`` is checked even when not sent — if the update
      flipped it, that is reported as a mismatch.
    - Returns ``success: True`` only when every verifiable field landed.
    """
    sent = {k: v for k, v in fields.items() if v is not None}
    unknown = sorted(set(sent) - set(_UPDATE_FIELDS))
    if unknown:
        return _err("INVALID_ARGS", f"Unknown field(s): {', '.join(unknown)}")
    if not sent:
        return _err("INVALID_ARGS", "Nothing to update.")
    if sent.get("sport") is not None and sent["sport"] not in SPORT_TYPE_MAP:
        return _err("INVALID_ARGS", f"Unknown sport {sent['sport']!r}. Allowed: {', '.join(SPORT_TYPE_MAP)}")
    if ("structure" in sent) and sent.get("tss_planned") is None:
        return _err(
            "VALIDATION_ERROR",
            "structure given without tss_planned; this fork never uploads the "
            "structure-estimated TSS. Pass tss_planned explicitly.",
        )

    try:
        before = await _read(workout_id)
    except RuntimeError as e:
        return _err("NOT_FOUND", str(e), workout_id=str(workout_id))

    structure_part = {k: sent[k] for k in _STRUCTURE_KEYS if k in sent}
    plain_part = {k: v for k, v in sent.items() if k not in structure_part}
    # A simplified `structure` needs its tss_planned in the same PUT
    # (upstream derives IF from it); structured_workout does not.
    if "structure" in structure_part and "tss_planned" in plain_part:
        structure_part["tss_planned"] = plain_part.pop("tss_planned")

    batches: list[tuple[str, dict[str, Any]]] = []
    if plain_part:
        batches.append(("fields", plain_part))
    if structure_part:
        batches.append(("structure", structure_part))

    steps: list[dict[str, Any]] = []
    report: dict[str, Any] = {}
    mismatched: list[str] = []
    after: dict[str, Any] = before

    for label, payload in batches:
        upstream = await tp_update_workout(workout_id=str(workout_id), **payload)
        if isinstance(upstream, dict) and upstream.get("isError"):
            steps.append({"step": label, "sent": sorted(payload), "upstream": upstream})
            return _err(
                upstream.get("error_code", "API_ERROR"),
                f"upstream tp_update_workout failed on step '{label}': {upstream.get('message')}",
                workout_id=str(workout_id), steps=steps, verified=report,
            )
        try:
            after = await _read(workout_id)
        except RuntimeError as e:
            steps.append({"step": label, "sent": sorted(payload), "upstream": upstream, "readback": "failed"})
            return _err("API_ERROR", str(e), workout_id=str(workout_id), steps=steps, verified=report)
        part_report, part_bad = _verify(payload, after)
        report.update(part_report)
        mismatched.extend(part_bad)
        steps.append({
            "step": label,
            "sent": sorted(payload),
            "upstream_message": upstream.get("message") if isinstance(upstream, dict) else None,
            "landed": not part_bad,
        })

    warnings: list[str] = []
    # Sport must not drift when it was not part of the update (TECH-16).
    if "sport" not in sent and before.get("sport") != after.get("sport"):
        report["sport"] = {"sent": None, "landed": after.get("sport"), "ok": False,
                           "note": f"sport drifted from {before.get('sport')!r} without being sent (TECH-16)"}
        mismatched.append("sport")
    if "tss_planned" in mismatched and after.get("sport") == "Run":
        warnings.append(
            "tss_planned mismatch on a Run: TP recomputes rTSS for pace-based runs "
            "(TECH-26); treat the landed value as authoritative for weekly totals."
        )
    if before.get("sport") == "Strength" or after.get("sport") == "Strength":
        warnings.append("Strength via tp_update_workout is the China-region text path; Strength Builder workouts are not visible here (TECH-18).")

    result: dict[str, Any] = {
        "success": not mismatched,
        "workout_id": str(workout_id),
        "title": after.get("title"),
        "sport": after.get("sport"),
        "date": after.get("date"),
        "verified": report,
        "unverifiable": sorted(k for k in sent if k in _UNVERIFIABLE),
        "steps": steps,
        "warnings": warnings,
    }
    if mismatched:
        result["isError"] = True
        result["error_code"] = "WRITE_NOT_LANDED"
        result["mismatched"] = sorted(set(mismatched))
        result["message"] = (
            f"Upstream reported success but {', '.join(sorted(set(mismatched)))} did not land "
            f"(see `verified`). Do NOT resend blindly — inspect first."
        )
    else:
        result["message"] = f"All {len(sent)} field(s) sent and {len(sent) - len(result['unverifiable'])} verified on read-back."
    return result


# ---------------------------------------------------------------------------
# In-place sport change (evaluation item B#6, TECH-38)
# ---------------------------------------------------------------------------


async def lo_set_sport(workout_id: str, sport: str, **fields: Any) -> dict[str, Any]:
    """Change a workout's sport in place (e.g. DayOff -> Swim) and verify.

    Keeps the workout id, so progress/history tracking is not broken and the
    "delete any workout = ask first" boundary is never touched. TECH-25 says
    ``sport`` alone does not take effect, so the current title is re-sent
    alongside it unless a new one is given.
    """
    if sport not in SPORT_TYPE_MAP:
        return _err("INVALID_ARGS", f"Unknown sport {sport!r}. Allowed: {', '.join(SPORT_TYPE_MAP)}")
    if sport == "Race":
        return _err("INVALID_ARGS", "sport='Race' cannot be set via update (lands as Other, TECH-25). Create it fresh.")
    try:
        before = await _read(workout_id)
    except RuntimeError as e:
        return _err("NOT_FOUND", str(e), workout_id=str(workout_id))
    payload = {k: v for k, v in fields.items() if v is not None}
    payload.setdefault("title", before.get("title"))
    payload["sport"] = sport
    result = await lo_update_workout_verified(workout_id, **payload)
    if isinstance(result, dict):
        result["sport_before"] = before.get("sport")
    return result


# ---------------------------------------------------------------------------
# Registration (the only hook into server.py)
# ---------------------------------------------------------------------------


def register_lo_tools(tools: list[Any], handlers: dict[str, Any]) -> None:
    """Append lo_* Tool definitions to ``tools`` and their handlers to ``handlers``.

    Schemas are derived from upstream's ``tp_update_workout`` at import time so
    they cannot drift from it.
    """
    from mcp.types import Tool  # local import: keep module importable in tests without server

    by_name = {t.name: t for t in tools}
    base = by_name["tp_update_workout"].input_schema
    props = dict(base.get("properties", {}))

    verified_props = dict(props)
    tools.append(Tool(
        name="lo_update_workout_verified",
        description=(
            "Update a workout AND prove it landed: reads the workout back after "
            "writing and compares every field (title, description, date, sport, "
            "duration, distance, TSS, structured_workout). Returns success only "
            "when everything matches; otherwise error_code WRITE_NOT_LANDED with "
            "a per-field sent/landed table. Automatically splits structured_workout "
            "into its own write (TECH-37) and detects sport drift (TECH-16). "
            "Prefer this over tp_update_workout for any planned-workout edit."
        ),
        input_schema={
            "type": "object",
            "properties": verified_props,
            "required": ["workout_id"],
        },
    ))

    set_sport_props = {k: v for k, v in props.items() if k not in ("structure", "structured_workout")}
    set_sport_props["sport"] = {
        "type": "string",
        "enum": [s for s in SPORT_TYPE_MAP if s != "Race"],
        "description": "New sport. The workout is converted in place; id is kept.",
    }
    tools.append(Tool(
        name="lo_set_sport",
        description=(
            "Convert a workout to another sport IN PLACE (e.g. DayOff -> Swim) "
            "instead of delete + create, keeping the workout id (TECH-38). "
            "Optionally set title/description/duration/distance/tss_planned in the "
            "same call. Verified by read-back like lo_update_workout_verified."
        ),
        input_schema={
            "type": "object",
            "properties": set_sport_props,
            "required": ["workout_id", "sport"],
        },
    ))

    async def _h_update_verified(args: dict[str, Any]) -> dict[str, Any]:
        a = dict(args)
        return await lo_update_workout_verified(a.pop("workout_id"), **a)

    async def _h_set_sport(args: dict[str, Any]) -> dict[str, Any]:
        a = dict(args)
        return await lo_set_sport(a.pop("workout_id"), a.pop("sport"), **a)

    handlers["lo_update_workout_verified"] = _h_update_verified
    handlers["lo_set_sport"] = _h_set_sport
