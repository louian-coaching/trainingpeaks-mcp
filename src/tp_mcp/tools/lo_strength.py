"""羅教練 fork: verified strength-workout update (feedback 2026-09-17, P4/P5).

``lo_update_strength_verified`` wraps the strength API the same way
``lo_update_workout_verified`` wraps ``tp_update_workout``:

* write → read back → compare a fingerprint (title, instructions, per-exercise
  set counts and prescribed values) → ``success`` only when it all landed
  (TECH-22 family: a write's own return value is never trusted).
* ``patch_exercises`` edits ONE exercise's sets/notes in place on the raw
  document, so changing one weight no longer means re-sending every block.

Wrapping detail: it uses the strength module's private helpers (``_access``,
``_headers``, ``_recount``…) because the public update tool rebuilds blocks
from library ids and cannot patch. Tests pin those imports.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from tp_mcp.client import TPClient
from tp_mcp.tools import strength as _s

logger = logging.getLogger(__name__)


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message, **extra}


def _norm(v: Any) -> str:
    """'12.0' == '12' == 12 for prescribed values."""
    s = str(v).strip()
    try:
        f = float(s)
        return str(int(f)) if f == int(f) else str(f)
    except ValueError:
        return s


def fingerprint(detail: dict[str, Any]) -> dict[str, Any]:
    """Compact, order-sensitive summary of a strength workout detail."""
    blocks = []
    for b in detail.get("blocks") or []:
        exs = []
        for ex in b.get("exercises") or []:
            sets = [{k: _norm(v) for k, v in (s.get("prescribed") or {}).items()} for s in ex.get("sets") or []]
            exs.append({"exercise": ex.get("exercise"), "notes": ex.get("notes"), "sets": sets})
        blocks.append({"type": b.get("type"), "title": b.get("title"), "notes": b.get("notes"), "exercises": exs})
    return {
        "title": detail.get("title"),
        "instructions": detail.get("instructions"),
        "total_sets": detail.get("total_sets"),
        "blocks": blocks,
    }


def _diff_fp(expected: dict[str, Any], landed: dict[str, Any]) -> list[str]:
    """Human-readable mismatches between two fingerprints."""
    out: list[str] = []
    for k in ("title", "instructions", "total_sets"):
        if expected.get(k) is not None and expected.get(k) != landed.get(k):
            out.append(f"{k}: expected {expected.get(k)!r}, landed {landed.get(k)!r}")
    eb, lb = expected.get("blocks") or [], landed.get("blocks") or []
    if len(eb) != len(lb):
        out.append(f"blocks: expected {len(eb)}, landed {len(lb)}")
    for bi, (b1, b2) in enumerate(zip(eb, lb, strict=False)):
        e1, e2 = b1.get("exercises") or [], b2.get("exercises") or []
        if len(e1) != len(e2):
            out.append(f"block[{bi}] exercises: expected {len(e1)}, landed {len(e2)}")
        for ei, (x1, x2) in enumerate(zip(e1, e2, strict=False)):
            tag = f"block[{bi}].exercise[{ei}] {x1.get('exercise')}"
            if x1.get("exercise") != x2.get("exercise"):
                out.append(f"{tag}: exercise landed as {x2.get('exercise')!r}")
            if x1.get("notes") is not None and x1.get("notes") != x2.get("notes"):
                out.append(f"{tag}: notes differ")
            if x1.get("sets") != x2.get("sets"):
                out.append(f"{tag}: sets expected {x1.get('sets')}, landed {x2.get('sets')}")
    return out


def _find_prescriptions(doc: dict[str, Any], exercise: str, block_index: int | None) -> list[tuple[int, int, dict]]:
    """(block_idx, exercise_idx, prescription) for exercises whose title matches."""
    hits = []
    needle = exercise.strip().lower()
    for bi, b in enumerate(doc.get("blocks") or []):
        if block_index is not None and bi != block_index:
            continue
        for ei, p in enumerate(b.get("prescriptions") or []):
            title = str((p.get("exercise") or {}).get("title") or "")
            if title.lower() == needle or needle in title.lower():
                hits.append((bi, ei, p))
    exact = [h for h in hits if str((h[2].get("exercise") or {}).get("title") or "").lower() == needle]
    return exact or hits


def _apply_patch(doc: dict[str, Any], patch: dict[str, Any]) -> str | None:
    """Mutate the raw doc for one patch item; return an error string or None."""
    exercise = patch.get("exercise")
    if not exercise:
        return "patch item needs `exercise` (title or substring)"
    hits = _find_prescriptions(doc, str(exercise), patch.get("block_index"))
    if not hits:
        return f"exercise {exercise!r} not found in the workout"
    if len(hits) > 1:
        where = [f"block[{bi}].exercise[{ei}]" for bi, ei, _ in hits]
        return f"exercise {exercise!r} matches {len(hits)} prescriptions ({', '.join(where)}); pass block_index"
    _, _, p = hits[0]

    if "notes" in patch:
        p["coachNotes"] = patch["notes"]

    set_values = patch.get("set_values")
    sets = patch.get("sets")
    if set_values and sets:
        return "give either set_values (apply to all sets) or sets (full list), not both"

    if set_values:
        for s in p.get("sets") or []:
            for param, val in set_values.items():
                if param not in _s._KNOWN_PARAMS:
                    return f"unknown parameter {param!r}"
                pv = next((x for x in s.get("parameterValues") or [] if x.get("parameter") == param), None)
                if pv is None:
                    s.setdefault("parameterValues", []).append({
                        "id": _s._u(), "parameter": param, "prescribedValue": str(val),
                        "executedValue": None, "inputFormat": _s._input_format(param),
                    })
                else:
                    pv["prescribedValue"] = str(val)
        _sync_columns(p)
    if sets is not None:
        if not isinstance(sets, list) or not sets:
            return "sets must be a non-empty list of {parameter: value} maps"
        for s in sets:
            bad = [k for k in s if k not in _s._KNOWN_PARAMS]
            if bad:
                return f"unknown parameter(s) {bad}"
        old_sets = p.get("sets") or []
        new_sets = []
        for i, s in enumerate(sets):
            base = old_sets[i] if i < len(old_sets) else {"id": _s._u(), "parameterValues": []}
            pvs = {x.get("parameter"): x for x in base.get("parameterValues") or []}
            values = []
            for param, val in s.items():
                pv = pvs.get(param) or {"id": _s._u(), "parameter": param, "executedValue": None,
                                        "inputFormat": _s._input_format(param)}
                pv["prescribedValue"] = str(val)
                values.append(pv)
            new_sets.append({**base, "parameterValues": values})
        p["sets"] = new_sets
        _sync_columns(p)
    return None


def _sync_columns(p: dict[str, Any]) -> None:
    cols: list[str] = []
    for s in p.get("sets") or []:
        for pv in s.get("parameterValues") or []:
            if pv.get("parameter") and pv["parameter"] not in cols:
                cols.append(pv["parameter"])
    p["parameters"] = [{"parameter": c, "inputFormat": _s._input_format(c)} for c in cols]


async def lo_update_strength_verified(
    workout_id: str,
    title: str | None = None,
    instructions: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
    mode: str = "replace",
    patch_exercises: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Update a strength workout and prove it landed.

    Either ``blocks`` (whole-structure replace/append, same shape as
    tp_update_strength_workout) or ``patch_exercises`` (surgical edits on the
    existing document), plus optional title/instructions. Reads the workout
    back afterwards and compares a fingerprint; ``success`` only when it
    matches. ``dry_run`` shows the expected fingerprint without writing.
    """
    wid = str(workout_id).strip()
    if not wid:
        return _err("INVALID_ARGS", "workout_id is required")
    if blocks is not None and patch_exercises:
        return _err("INVALID_ARGS", "give either blocks or patch_exercises, not both")
    if blocks is None and not patch_exercises and title is None and instructions is None:
        return _err("INVALID_ARGS", "nothing to update")
    if mode not in ("replace", "append"):
        return _err("INVALID_ARGS", "mode must be 'replace' or 'append'")
    if blocks is not None:
        invalid = _s._validate_blocks(blocks)
        if invalid:
            return _err("VALIDATION_ERROR", invalid)

    async with TPClient() as client:
        _, access, err = await _s._access(client)
        if err:
            return err
        try:
            async with httpx.AsyncClient(timeout=_s.STRENGTH_TIMEOUT) as h:
                r = await h.get(f"{_s.STRENGTH_API_BASE}/rx/activity/v1/workouts/{wid}", headers=_s._headers(access))
                if r.status_code != 200:
                    return _s._map_status(r.status_code, r.text)
                doc = r.json().get("data") or {}
                if not doc:
                    return _err("NOT_FOUND", "Strength workout not found.")
                before_fp = fingerprint(_s._fmt_workout_detail(doc))

                changed: list[str] = []
                if blocks is not None:
                    catalogue = _s._catalogue()
                    new_blocks = [{
                        "id": _s._u(), "blockType": b.get("type", "SingleExercise"), "title": b.get("title"),
                        "coachNotes": b.get("notes"),
                        "prescriptions": [_s._build_prescription(ex, catalogue) for ex in b["exercises"]],
                    } for b in blocks]
                    doc["blocks"] = (doc.get("blocks") or []) + new_blocks if mode == "append" else new_blocks
                    changed.append(f"blocks ({mode})")
                for i, patch in enumerate(patch_exercises or []):
                    perr = _apply_patch(doc, patch)
                    if perr:
                        return _err("INVALID_ARGS", f"patch_exercises[{i}]: {perr}")
                    changed.append(f"patch {patch.get('exercise')}")
                if title is not None:
                    doc["title"] = str(title).strip()
                    changed.append("title")
                if instructions is not None:
                    doc["instructions"] = instructions
                    changed.append("instructions")
                doc["snapshot"] = _s._recount(doc.get("blocks") or [])
                expected_fp = fingerprint(_s._fmt_workout_detail(doc))

                if dry_run:
                    return {"workout_id": wid, "dry_run": True, "changed": changed,
                            "expected": expected_fp, "diff_from_current": _diff_fp(expected_fp, before_fp)}

                r = await h.post(f"{_s.STRENGTH_API_BASE}/rx/activity/v1/workouts/save",
                                 headers=_s._headers(access), json=doc)
                if r.status_code != 200:
                    try:
                        errs = r.json().get("errors")
                    except Exception:  # noqa: BLE001
                        errs = None
                    if errs:
                        return _err("API_ERROR", f"Strength API rejected the update: {errs}")
                    return _s._map_status(r.status_code, r.text)
                returned = str((r.json().get("data") or {}).get("id") or wid)

                r2 = await h.get(f"{_s.STRENGTH_API_BASE}/rx/activity/v1/workouts/{wid}", headers=_s._headers(access))
                if r2.status_code != 200:
                    return _err("API_ERROR", f"write accepted but read-back failed: HTTP {r2.status_code}",
                                workout_id=wid, changed=changed)
                after_detail = _s._fmt_workout_detail(r2.json().get("data") or {})
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Strength update timed out.")
        except httpx.RequestError:
            logger.exception("Network error in lo_update_strength_verified")
            return _err("NETWORK_ERROR", "A network error occurred.")

    landed_fp = fingerprint(after_detail)
    mismatches = _diff_fp(expected_fp, landed_fp)
    result: dict[str, Any] = {
        "success": not mismatches and returned == wid,
        "workout_id": wid,
        "title": after_detail.get("title"),
        "date": after_detail.get("date"),
        "changed": changed,
        "total_sets": after_detail.get("total_sets"),
        "blocks": len(after_detail.get("blocks") or []),
        "mismatches": mismatches,
    }
    if returned != wid:
        result["warning"] = f"server returned id {returned}, expected {wid} — a duplicate may exist"
    if not result["success"]:
        result["isError"] = True
        result["error_code"] = "WRITE_NOT_LANDED"
        result["message"] = "Strength update did not fully land; see mismatches. Do not resend blindly."
        result["expected"] = expected_fp
        result["landed"] = landed_fp
    else:
        result["message"] = f"{len(changed)} change(s) written and verified on read-back."
    return result


def register_lo_strength(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(Tool(
        name="lo_update_strength_verified",
        description=(
            "Update a Strength Builder workout AND prove it landed (read-back "
            "fingerprint: title, instructions, every exercise's set count and "
            "prescribed values). Use `patch_exercises` to change ONE exercise's "
            "sets/notes without re-sending all blocks (e.g. one weight from 12.5 to 12); "
            "use `blocks` for a full replace/append like tp_update_strength_workout. "
            "success only when the fingerprint matches; otherwise WRITE_NOT_LANDED "
            "with expected vs landed. Closes TECH-12/22 for strength writes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workout_id": {"type": "string"},
                "title": {"type": "string"},
                "instructions": {"type": "string"},
                "blocks": {"type": "array", "items": {"type": "object"},
                           "description": "Same shape as tp_update_strength_workout.blocks"},
                "mode": {"type": "string", "enum": ["replace", "append"], "default": "replace"},
                "patch_exercises": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Surgical edits: [{exercise: '<title or substring>', block_index?: int, "
                        "set_values?: {WeightKg: 12}  (applied to every set), "
                        "sets?: [{Reps: 10, WeightKg: 12}, ...] (full set list), notes?: '...'}]"
                    ),
                },
                "dry_run": {"type": "boolean", "default": False},
            },
            "required": ["workout_id"],
        },
    ))

    async def _h(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_update_strength_verified(
            workout_id=args["workout_id"], title=args.get("title"), instructions=args.get("instructions"),
            blocks=args.get("blocks"), mode=args.get("mode", "replace"),
            patch_exercises=args.get("patch_exercises"), dry_run=bool(args.get("dry_run", False)),
        )

    handlers["lo_update_strength_verified"] = _h
