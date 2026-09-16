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
# payload_file helpers
# ---------------------------------------------------------------------------


def load_payload_file(path: str) -> dict[str, Any]:
    """Read a JSON object from disk; raises ValueError with a readable message."""
    import json

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise ValueError(f"payload_file unreadable: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("payload_file must contain a JSON object")
    return data


def peek_payload_athlete(path: str) -> str | None:
    """Return the "athlete" value inside a payload_file, or None (never raises)."""
    try:
        val = load_payload_file(path).get("athlete")
    except ValueError:
        return None
    return str(val) if val not in (None, "") else None


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
_UNVERIFIABLE = {"subtype_id", "tags", "athlete_comment", "coach_comment", "is_hidden"}

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
    classes: list[str] = []
    cadence_targets = 0
    if isinstance(steps, list) and steps:
        if isinstance(steps[0], dict):
            first_begin = steps[0].get("begin")
        if isinstance(steps[-1], dict):
            last_end = steps[-1].get("end")
        for blk in steps:
            for st in (blk.get("steps") or []) if isinstance(blk, dict) else []:
                if isinstance(st, dict):
                    classes.append(str(st.get("intensityClass") or "?")[:1])
                    for t in st.get("targets") or []:
                        if isinstance(t, dict) and t.get("unit") == "roundOrStridePerMinute":
                            cadence_targets += 1
    return {
        "primaryLengthMetric": sw.get("primaryLengthMetric"),
        "primaryIntensityMetric": sw.get("primaryIntensityMetric"),
        "blocks": len(steps) if isinstance(steps, list) else 0,
        "begin": first_begin,
        "end": last_end,
        "polyline_points": len(poly) if isinstance(poly, list) else 0,
        # w/a/r/c per inner step in order — catches class edits (e.g. rest -> warmUp)
        "classes": "".join(classes),
        "cadence_targets": cadence_targets,
    }


def _simplified_total_seconds(structure: Any) -> int | None:
    """Total seconds of a simplified `structure` (steps × reps), or None."""
    if isinstance(structure, str):
        import json

        try:
            structure = json.loads(structure)
        except ValueError:
            return None
    if not isinstance(structure, dict):
        return None
    total = 0
    for step in structure.get("steps") or []:
        if not isinstance(step, dict):
            return None
        if step.get("type") == "repetition":
            reps = step.get("reps") or 1
            inner = 0
            for s in step.get("steps") or []:
                if not isinstance(s, dict) or not isinstance(s.get("duration_seconds"), (int, float)):
                    return None
                inner += s["duration_seconds"]
            total += inner * reps
        elif isinstance(step.get("duration_seconds"), (int, float)):
            total += step["duration_seconds"]
        else:
            return None
    return int(total)


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
    if field in ("structured_workout", "structure"):
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
    if field == "structure":
        # simplified structure is converted upstream; verify the converted
        # result is present and its total length matches what we described.
        if not isinstance(landed, dict) or not landed.get("blocks"):
            return False
        total = _simplified_total_seconds(sent)
        return total is None or landed.get("end") == total
    if field in ("feeling", "rpe"):
        return _num_close(sent, landed, 0)
    return sent == landed


_TEXT_FIELDS = ("description", "title", "tags", "athlete_comment", "coach_comment")
_DIFF_MAX_LINES = 40


def _sha8(text: Any) -> str:
    import hashlib

    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:8]


def _text_diff(sent: str, landed: str) -> list[str]:
    """Unified diff (landed -> sent), capped, so a mismatch shows *what* differs."""
    import difflib

    lines = list(difflib.unified_diff(
        (landed or "").splitlines(), (sent or "").splitlines(),
        fromfile="landed", tofile="sent", lineterm="", n=1,
    ))
    if len(lines) > _DIFF_MAX_LINES:
        lines = lines[:_DIFF_MAX_LINES] + [f"... ({len(lines) - _DIFF_MAX_LINES} more lines)"]
    return lines


def _verify(sent: dict[str, Any], detail: dict[str, Any], verbose: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Field-by-field comparison. Returns (report, mismatched_field_names).

    Text fields are reported compactly: ``{ok, len, sha8}`` when they match,
    a capped unified diff when they do not. ``verbose=True`` adds the full
    sent/landed text.
    """
    report: dict[str, Any] = {}
    bad: list[str] = []
    for field, value in sent.items():
        if field in _UNVERIFIABLE:
            report[field] = {"sent": value, "landed": None, "ok": None, "note": "not echoed by tp_get_workout"}
            continue
        landed = _landed_value(field, detail)
        ok = _compare(field, value, landed)
        if field in _TEXT_FIELDS and isinstance(value, str):
            entry: dict[str, Any] = {"ok": ok, "len": len(value), "sha8": _sha8(value)}
            if not ok:
                entry["landed_len"] = len(landed or "")
                entry["landed_sha8"] = _sha8(landed)
                entry["diff"] = _text_diff(value, landed or "")
                bad.append(field)
            if verbose:
                entry["sent"] = value
                entry["landed"] = landed
            report[field] = entry
            continue
        if field == "structured_workout":
            shown_sent: Any = _structure_fingerprint(value)
        elif field == "structure":
            shown_sent = {"total_seconds": _simplified_total_seconds(value)}
        else:
            shown_sent = value
        report[field] = {"sent": shown_sent, "landed": landed, "ok": ok}
        if not ok:
            bad.append(field)
    return report, bad


async def _read(workout_id: str) -> dict[str, Any]:
    detail = await tp_get_workout(workout_id=str(workout_id))
    if isinstance(detail, dict) and detail.get("isError"):
        raise RuntimeError(f"read-back failed: {detail.get('message')}")
    return detail


async def lo_update_workout_verified(workout_id: str, verbose: bool = False, **fields: Any) -> dict[str, Any]:
    """Public wrapper: same as ``_update_verified`` without the ``_after`` detail."""
    result = await _update_verified(workout_id, verbose=verbose, **fields)
    result.pop("_after", None)
    return result


async def _update_verified(workout_id: str, verbose: bool = False, **fields: Any) -> dict[str, Any]:
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
        part_report, part_bad = _verify(payload, after, verbose=verbose)
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
        warnings.append(
            "Strength via tp_update_workout is the China-region text path; "
            "Strength Builder workouts are not visible here (TECH-18)."
        )

    result: dict[str, Any] = {
        "_after": after,  # consumed by lo_update_workouts_batch, stripped before returning
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
        n_verified = len(sent) - len(result["unverifiable"])
        result["message"] = f"All {len(sent)} field(s) sent and {n_verified} verified on read-back."
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
# Batch update (feedback 2026-09-16 #3) and week read-back for validate (#2)
# ---------------------------------------------------------------------------

_STRUCTURE_SPORTS = {"Bike", "Run", "Brick", "MtnBike"}


def _row(result: dict[str, Any]) -> dict[str, Any]:
    """One compact line per workout for batch output."""
    row = {
        "workout_id": result.get("workout_id"),
        "date": result.get("date"),
        "sport": result.get("sport"),
        "title": result.get("title"),
        "success": bool(result.get("success")),
        "steps": [s.get("step") for s in result.get("steps") or []],
    }
    if result.get("isError"):
        row["error_code"] = result.get("error_code")
        row["message"] = result.get("message")
    if result.get("mismatched"):
        row["mismatched"] = result["mismatched"]
        row["verified"] = {k: v for k, v in (result.get("verified") or {}).items() if v.get("ok") is False}
    if result.get("warnings"):
        row["warnings"] = result["warnings"]
    return row


def _write_json(path: str, data: Any) -> None:
    import json

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


async def lo_update_workouts_batch(
    updates: list[dict[str, Any]],
    on_error: str = "stop",
    verbose: bool = False,
    readback_save_to: str | None = None,
) -> dict[str, Any]:
    """Run lo_update_workout_verified over a list; one round-trip instead of N.

    Each item: ``{"workout_id": ..., <fields as lo_update_workout_verified>}``.
    Sequential, never retries. ``on_error="stop"`` (default) halts at the first
    row that is not fully verified; remaining rows are reported as
    ``not_attempted``. With ``readback_save_to`` the post-update details are
    written in validate_week.py shape (structured_workout included).
    """
    from tp_mcp.tools.workouts_batch import _flatten_readback

    if not isinstance(updates, list) or not updates:
        return _err("INVALID_ARGS", "updates must be a non-empty list")
    if on_error not in ("stop", "continue"):
        return _err("INVALID_ARGS", "on_error must be 'stop' or 'continue'")
    for i, u in enumerate(updates):
        if not isinstance(u, dict) or not u.get("workout_id"):
            return _err("INVALID_ARGS", f"updates[{i}] needs a workout_id")

    rows: list[dict[str, Any]] = []
    readback: list[dict[str, Any]] = []
    summary = {"total": len(updates), "verified": 0, "failed": 0, "not_attempted": 0}
    halted = False
    for i, u in enumerate(updates):
        if halted:
            rows.append({"workout_id": str(u["workout_id"]), "status": "not_attempted"})
            summary["not_attempted"] += 1
            continue
        fields = {k: v for k, v in u.items() if k not in ("workout_id", "athlete")}
        try:
            res = await _update_verified(str(u["workout_id"]), verbose=verbose, **fields)
        except Exception as e:  # noqa: BLE001 — a crash on one row must not lose the others
            res = _err("API_ERROR", f"exception: {e}", workout_id=str(u["workout_id"]))
        after = res.pop("_after", None)
        if isinstance(after, dict):
            readback.append(_flatten_readback(after))
        rows.append({"index": i, **_row(res)})
        if res.get("success"):
            summary["verified"] += 1
        else:
            summary["failed"] += 1
            if on_error == "stop":
                halted = True

    out: dict[str, Any] = {
        "success": summary["failed"] == 0 and summary["not_attempted"] == 0,
        "summary": summary,
        "results": rows,
    }
    if readback_save_to:
        try:
            _write_json(readback_save_to, {"workouts": readback})
            out["readback_json_path"] = readback_save_to
        except OSError as e:
            out["warnings"] = [f"readback_save_to failed: {e}"]
    if not out["success"]:
        out["isError"] = True
        out["error_code"] = "WRITE_NOT_LANDED"
        out["message"] = (
            f"{summary['failed']} of {summary['total']} update(s) did not fully land"
            + (f"; {summary['not_attempted']} not attempted (on_error=stop)" if summary["not_attempted"] else "")
            + ". Inspect results[].verified before resending."
        )
    return out


async def lo_get_week_for_validate(
    start_date: str,
    end_date: str,
    save_to: str,
    workout_filter: str = "planned",
) -> dict[str, Any]:
    """Read a date range back in the exact shape validate_week.py consumes.

    tp_get_workouts (list) lacks structured_workout, so R2/R3/R4/R12 cannot be
    checked from it. This tool lists the range, then fetches the detail of
    every Bike/Run/Brick/MtnBike workout (the sports that carry a structure),
    flattens everything with the same helper the batch-create tool uses, and
    writes the file. Only a summary is returned.
    """
    from tp_mcp.tools.workouts import tp_get_workouts
    from tp_mcp.tools.workouts_batch import _flatten_readback

    listed = await tp_get_workouts(start_date=start_date, end_date=end_date, workout_filter=workout_filter)
    if isinstance(listed, dict) and listed.get("isError"):
        return listed
    workouts = listed.get("workouts") or []
    out_rows: list[dict[str, Any]] = []
    detail_failures: list[str] = []
    fetched = 0
    for w in workouts:
        wid = str(w.get("id"))
        if w.get("sport") in _STRUCTURE_SPORTS:
            detail = await tp_get_workout(workout_id=wid)
            if isinstance(detail, dict) and not detail.get("isError"):
                out_rows.append(_flatten_readback(detail))
                fetched += 1
                continue
            detail_failures.append(wid)
        row = dict(w)
        row.setdefault("type", "planned")
        out_rows.append(row)
    try:
        _write_json(save_to, {"workouts": out_rows, "date_range": {"start": start_date, "end": end_date}})
    except OSError as e:
        return _err("API_ERROR", f"save_to failed: {e}")
    per_sport: dict[str, int] = {}
    for r in out_rows:
        per_sport[str(r.get("sport"))] = per_sport.get(str(r.get("sport")), 0) + 1
    return {
        "success": True,
        "saved_to": save_to,
        "date_range": {"start": start_date, "end": end_date},
        "count": len(out_rows),
        "with_structure": sum(1 for r in out_rows if r.get("structured_workout")),
        "detail_calls": fetched,
        "detail_failures": detail_failures,
        "per_sport": per_sport,
        "workouts": [
            {"id": r.get("id"), "date": r.get("date"), "sport": r.get("sport"), "title": r.get("title"),
             "tss_planned": r.get("tss_planned")}
            for r in out_rows
        ],
        "next": f"python3 tp-ai-layer/tools/validate_week.py {save_to} <flags>",
    }


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
    verified_props["verbose"] = {
        "type": "boolean",
        "default": False,
        "description": (
            "Also return full sent/landed text for text fields "
            "(default: only ok/len/sha8, or a diff on mismatch)."
        ),
    }
    verified_props["payload_file"] = {
        "type": "string",
        "description": (
            "Absolute path to a JSON file holding the fields to update (same keys as "
            "this tool; may include workout_id). File values are used unless the same "
            "argument is passed explicitly. Keeps a full structured_workout out of the "
            "model context."
        ),
    }
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
            "required": [],
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

    tools.append(Tool(
        name="lo_update_workouts_batch",
        description=(
            "Update MANY workouts in one call, each verified by read-back exactly "
            "like lo_update_workout_verified (split writes, sport-drift check, "
            "per-field sent/landed). Pass `updates` inline or a `payload_file` "
            "produced by `tp_build.py --week --update`. Sequential, never retries; "
            "on_error=stop halts at the first row that did not land. With "
            "readback_save_to the post-update details are written in "
            "validate_week.py shape (structure included) so Step 5 needs no extra read."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "athlete": props.get("athlete", {"type": "string"}),
                "updates": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Each item: {workout_id, ...fields of lo_update_workout_verified}.",
                },
                "payload_file": {
                    "type": "string",
                    "description": (
                        "Absolute path to a JSON file {athlete?, updates:[...], on_error?, "
                        "readback_save_to?}. File values are used unless the same argument is "
                        "passed explicitly."
                    ),
                },
                "on_error": {"type": "string", "enum": ["stop", "continue"], "default": "stop"},
                "verbose": {"type": "boolean", "default": False},
                "readback_save_to": {
                    "type": "string",
                    "description": "Absolute path; writes {workouts:[...]} in validate_week.py shape.",
                },
            },
            "required": [],
        },
    ))

    tools.append(Tool(
        name="lo_get_week_for_validate",
        description=(
            "Read a date range back in the exact shape validate_week.py consumes, "
            "structured_workout INCLUDED (tp_get_workouts alone lacks it, which is why "
            "R2/R3/R4/R12 kept landing in the skipped list). Lists the range, fetches "
            "the detail of every Bike/Run/Brick/MtnBike workout, flattens, writes "
            "save_to, returns only a summary. Use for sop-weekly Step 5 after any "
            "manual edits, or whenever a week must be re-validated."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "athlete": props.get("athlete", {"type": "string"}),
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "save_to": {"type": "string", "description": "Absolute path for the validate-ready JSON."},
                "type": {"type": "string", "enum": ["planned", "completed", "all"], "default": "planned"},
            },
            "required": ["start_date", "end_date", "save_to"],
        },
    ))

    async def _h_update_batch(args: dict[str, Any]) -> dict[str, Any]:
        a = dict(args)
        payload_file = a.pop("payload_file", None)
        if payload_file:
            try:
                file_args = load_payload_file(payload_file)
            except ValueError as e:
                return _err("INVALID_ARGS", str(e))
            file_args.pop("athlete", None)
            a = {**file_args, **a}
        if "updates" not in a:
            return _err("INVALID_ARGS", "updates (or payload_file containing updates) is required")
        return await lo_update_workouts_batch(
            updates=a["updates"],
            on_error=a.get("on_error", "stop"),
            verbose=bool(a.get("verbose", False)),
            readback_save_to=a.get("readback_save_to"),
        )

    async def _h_get_week(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_get_week_for_validate(
            start_date=args["start_date"], end_date=args["end_date"],
            save_to=args["save_to"], workout_filter=args.get("type", "planned"),
        )

    handlers["lo_update_workouts_batch"] = _h_update_batch
    handlers["lo_get_week_for_validate"] = _h_get_week

    async def _h_update_verified(args: dict[str, Any]) -> dict[str, Any]:
        a = dict(args)
        payload_file = a.pop("payload_file", None)
        if payload_file:
            try:
                file_args = load_payload_file(payload_file)
            except ValueError as e:
                return _err("INVALID_ARGS", str(e))
            file_args.pop("athlete", None)
            a = {**file_args, **a}
        if "workout_id" not in a:
            return _err("INVALID_ARGS", "workout_id is required (directly or via payload_file)")
        return await lo_update_workout_verified(a.pop("workout_id"), **a)

    async def _h_set_sport(args: dict[str, Any]) -> dict[str, Any]:
        a = dict(args)
        return await lo_set_sport(a.pop("workout_id"), a.pop("sport"), **a)

    handlers["lo_update_workout_verified"] = _h_update_verified
    handlers["lo_set_sport"] = _h_set_sport
