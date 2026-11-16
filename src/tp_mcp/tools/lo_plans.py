"""Training-plan authoring tools (FORK 2026-11-12, 教練選單裁示).

  lo_plan_get_workouts     — plan workouts with ids and week/day, optional full structure
  lo_plan_create           — new PRIVATE plan (title, optional description / language)
  lo_plan_update_meta      — title / description / language of a plan
  lo_plan_add_workouts     — batch-create workouts inside a plan (week/day addressing)
  lo_plan_update_workout   — change one plan workout (read → merge → full PUT → read back)
  lo_plan_delete_workouts  — delete plan workouts, each verified gone

Upstream's plans.py (PR #142) only reads plans and copies them onto an athlete.
These tools write INTO a plan, which is what selling plans needs.

Endpoints — read from the TP web app bundle (release-20261111) and verified live
on 2026-11-12 against a private scratch plan (696132 「_AI測試_可刪」):
  POST   /plans/v1/plans                       {title, isDynamic:false} → plan object
  PUT    /plans/v1/plans/{id}                  full plan object
  POST   /plans/v1/plans/{id}/workouts         workout object, athleteId = planPersonId
  PUT    /plans/v1/plans/{id}/workouts/{wid}   FULL workout object
  DELETE /plans/v1/plans/{id}/workouts/{wid}   → true

What the live test established:
  * A plan workout is an ordinary workout hung on the plan's virtual person
    (``planPersonId``); the payload is the same shape as a calendar workout.
  * ``plan.startDate`` is the date of the EARLIEST workout and moves when an
    earlier one is added or the earliest is removed. Week 1 = the Monday-week that
    holds startDate, so an empty plan must get its week-1 content first.
  * A PARTIAL PUT blanks every field it leaves out (description, TSS and duration
    were wiped). Updates therefore read the workout, merge, and PUT all of it.
  * ``structure`` goes up as a JSON string and comes back as an object.

Guards:
  * Published plans (isPublic) are refused unless ``allow_public=true`` — the coach
    approves each such change (11/12 menu decision).
  * Nothing here touches price, isPublic, isSearchable or the store listing, and
    nothing deletes a plan: those stay in the TP web UI.
  * /plans/v1/commands/applyplan stays blocked in the HTTP client.
  * No automatic retry: a write with no answer is reported, then looked up in the
    read-back, never sent twice.

File hand-off (FORK 2026-11-12, TECH-62): long Chinese descriptions passed as tool
arguments had to be re-typed by the model and came back with wrong characters.
``payload_file`` (add / update) reads the workouts from a JSON file on this machine and
``save_to`` (get) writes the full read-back to one, so text goes file → TP → file and
is compared by a script, never by eye.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date as date_type
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from tp_mcp.client import TPClient
from tp_mcp.tools._validation import format_validation_error
from tp_mcp.tools.workouts import (
    SPORT_TYPE_MAP,
    _decode_structured_workout,
    _encode_structured_workout,
    _prepare_structure_payload,
)

logger = logging.getLogger("tp-mcp")

_BASE = "/plans/v1/plans"
MAX_BATCH = 40
_ABORT_AFTER_FAILURES = 3
_HEAD_CHARS = 60
_SPORT_BY_TYPE: dict[int, str] = {v[1]: k for k, v in SPORT_TYPE_MAP.items()}
_UNKNOWN_OUTCOME = {"NETWORK_ERROR", "WRITE_OUTCOME_UNKNOWN", "UNPARSEABLE_RESPONSE"}

_LOCK_MESSAGE = (
    "This plan is published (isPublic=true). Writes are refused unless allow_public=true, "
    "and allow_public needs the coach's explicit approval for this specific change: list "
    "what will change, ask, then retry."
)


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message, **extra}


def _read_json_file(path: str) -> tuple[Any, dict[str, Any] | None]:
    """Load a JSON hand-off file; (data, None) or (None, error)."""
    try:
        return json.loads(Path(os.path.expanduser(path)).read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, _err("VALIDATION_ERROR", f"payload_file not found: {path}")
    except (OSError, ValueError) as e:
        return None, _err("VALIDATION_ERROR", f"payload_file could not be read as JSON: {e}")


def _api_err(response: Any, what: str) -> dict[str, Any]:
    code = response.error_code.value if response.error_code else "API_ERROR"
    return _err(code, f"{what}: {response.message}")


def _plan_id(value: Any) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _monday(d: date_type) -> date_type:
    return d - timedelta(days=d.weekday())


def _iso_day(value: Any) -> date_type | None:
    try:
        return date_type.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _anchor(plan: dict[str, Any]) -> date_type | None:
    """Monday of week 1, or None for an empty plan."""
    start = _iso_day(plan.get("startDate"))
    return _monday(start) if start else None


def _date_for(anchor: date_type, week: int, day: int) -> date_type:
    return anchor + timedelta(days=(week - 1) * 7 + (day - 1))


def _week_day(anchor: date_type | None, d: date_type | None) -> tuple[int | None, int | None]:
    if anchor is None or d is None:
        return None, None
    rel = (d - anchor).days
    return rel // 7 + 1, rel % 7 + 1


def _brief(plan: dict[str, Any]) -> dict[str, Any]:
    anchor = _anchor(plan)
    return {
        "plan_id": plan.get("planId"),
        "title": (plan.get("title") or "").strip(),
        "is_public": bool(plan.get("isPublic")),
        "weeks": plan.get("weekCount"),
        "workouts": plan.get("workoutCount"),
        "week1_monday": anchor.isoformat() if anchor else None,
    }


def _lock(plan: dict[str, Any], allow_public: bool) -> dict[str, Any] | None:
    if plan.get("isPublic") and not allow_public:
        return _err("PUBLIC_PLAN_LOCKED", _LOCK_MESSAGE, plan=_brief(plan))
    return None


def _row(w: dict[str, Any], anchor: date_type | None, full: bool) -> dict[str, Any]:
    week, day = _week_day(anchor, _iso_day(w.get("workoutDay")))
    hours = w.get("totalTimePlanned")
    metres = w.get("distancePlanned")
    desc = w.get("description")
    row: dict[str, Any] = {
        "workout_id": w.get("workoutId"),
        "week": week,
        "day": day,
        "sport": _SPORT_BY_TYPE.get(w.get("workoutTypeValueId"), str(w.get("workoutTypeValueId"))),
        "title": (w.get("title") or "").strip(),
        "duration_min": round(hours * 60) if hours else None,
        "distance_km": round(metres / 1000, 2) if metres else None,
        "tss": w.get("tssPlanned"),
        "has_structure": w.get("structure") is not None,
    }
    if full:
        row["description"] = desc
        row["structured_workout"] = _decode_structured_workout(w.get("structure"))
    else:
        row["description_head"] = (desc or "")[:_HEAD_CHARS] or None
    return row


def _per_week(rows: list[dict[str, Any]]) -> dict[str, int]:
    per: dict[str, int] = {}
    for r in rows:
        k = str(r.get("week"))
        per[k] = per.get(k, 0) + 1
    return per


def _slim(row: dict[str, Any]) -> dict[str, Any]:
    """A full row cut back to the brief shape (after it was saved to a file)."""
    out = {k: v for k, v in row.items() if k not in ("description", "structured_workout")}
    out["description_head"] = (row.get("description") or "")[:_HEAD_CHARS] or None
    return out


async def _load(
    client: TPClient, plan_id: int, with_workouts: bool = True,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
    """(plan, workouts, error). Workouts are empty for an empty plan."""
    r = await client.get(f"{_BASE}/{plan_id}")
    if r.is_error:
        return None, [], _api_err(r, "read plan")
    plan = r.data
    if not isinstance(plan, dict) or plan.get("planId") != plan_id:
        return None, [], _err("API_ERROR", f"Plan {plan_id} was not returned by TrainingPeaks.")
    anchor = _anchor(plan)
    if not with_workouts or anchor is None:
        return plan, [], None
    lo = anchor - timedelta(days=7)
    hi = anchor + timedelta(days=int(plan.get("dayCount") or 0) + 21)
    wr = await client.get(f"{_BASE}/{plan_id}/workouts/{lo.isoformat()}/{hi.isoformat()}")
    if wr.is_error:
        return None, [], _api_err(wr, "read plan workouts")
    rows = wr.data if isinstance(wr.data, list) else []
    return plan, [w for w in rows if isinstance(w, dict)], None


def _count_warning(plan: dict[str, Any], workouts: list[dict[str, Any]]) -> str | None:
    expected = plan.get("workoutCount")
    if isinstance(expected, int) and expected != len(workouts):
        return (f"TP reports {expected} workouts in this plan but the read-back returned "
                f"{len(workouts)} — treat this read as incomplete.")
    return None


class _Item(BaseModel):
    week: int = Field(ge=1, le=104)
    day: int = Field(ge=1, le=7)
    sport: str
    title: str = Field(min_length=1, max_length=200)
    duration_minutes: float | None = Field(default=None, gt=0, le=1440)
    description: str | None = None
    distance_km: float | None = Field(default=None, gt=0)
    tss_planned: float | None = Field(default=None, ge=0)
    if_planned: float | None = Field(default=None, gt=0, le=2)
    structure: dict[str, Any] | str | None = None
    structured_workout: dict[str, Any] | None = None

    @field_validator("sport")
    @classmethod
    def _known_sport(cls, v: str) -> str:
        if v not in SPORT_TYPE_MAP:
            raise ValueError(f"unknown sport {v!r}; use one of {', '.join(SPORT_TYPE_MAP)}")
        return v

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title is blank")
        return v.strip()

    @model_validator(mode="after")
    def _structure_rules(self) -> _Item:
        if self.structure is not None and self.structured_workout is not None:
            raise ValueError("give structure or structured_workout, not both")
        if (self.structure is not None or self.structured_workout is not None) and self.tss_planned is None:
            raise ValueError("a structured workout needs tss_planned (this fork never uploads an estimated TSS)")
        return self


def _structure_string(
    structure: dict[str, Any] | str | None, structured_workout: dict[str, Any] | None,
) -> tuple[str | None, float | None, str | None]:
    """(wire JSON string, duration minutes derived from it, error)."""
    if structure is not None:
        sp = _prepare_structure_payload(structure)
        if sp.error is not None:
            return None, None, sp.error
        return json.dumps(sp.wire_structure), sp.duration_minutes, None
    raw, raw_error = _encode_structured_workout(structured_workout)
    return raw, None, raw_error


def _payload(item: _Item, plan_person_id: int, day: date_type) -> tuple[dict[str, Any] | None, str | None]:
    wire, derived_minutes, error = _structure_string(item.structure, item.structured_workout)
    if error:
        return None, error
    family_id, type_id = SPORT_TYPE_MAP[item.sport]
    payload: dict[str, Any] = {
        "workoutId": 0,
        "athleteId": plan_person_id,
        "workoutDay": f"{day.isoformat()}T00:00:00",
        "workoutTypeFamilyId": family_id,
        "workoutTypeValueId": type_id,
        "title": item.title,
    }
    minutes = item.duration_minutes if item.duration_minutes is not None else derived_minutes
    if minutes is not None:
        payload["totalTimePlanned"] = minutes / 60.0
    if item.description:
        payload["description"] = item.description
    if item.distance_km is not None:
        payload["distancePlanned"] = item.distance_km * 1000
    if item.tss_planned is not None:
        payload["tssPlanned"] = item.tss_planned
    if item.if_planned is not None:
        payload["ifPlanned"] = item.if_planned
    if wire is not None:
        payload["structure"] = wire
    return payload, None


def _norm_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").strip()


def _close(a: Any, b: Any, tolerance: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return False


def _mismatch(sent: dict[str, Any], got: dict[str, Any]) -> list[str]:
    """Fields of ``got`` (read back) that do not hold what ``sent`` asked for."""
    bad: list[str] = []
    if _norm_text(sent.get("title")) != _norm_text(got.get("title")):
        bad.append("title")
    if _norm_text(sent.get("description")) != _norm_text(got.get("description")):
        bad.append("description")
    if str(sent.get("workoutDay") or "")[:10] != str(got.get("workoutDay") or "")[:10]:
        bad.append("day")
    if sent.get("workoutTypeValueId") != got.get("workoutTypeValueId"):
        bad.append("sport")
    if not _close(sent.get("totalTimePlanned"), got.get("totalTimePlanned"), 0.01):
        bad.append("duration")
    if not _close(sent.get("distancePlanned"), got.get("distancePlanned"), 1.0):
        bad.append("distance")
    if not _close(sent.get("tssPlanned"), got.get("tssPlanned"), 0.1):
        bad.append("tss")
    if (sent.get("structure") is not None) != (got.get("structure") is not None):
        bad.append("structure")
    return bad


def _dup_key(w: dict[str, Any]) -> tuple[str, Any, str]:
    return (str(w.get("workoutDay") or "")[:10], w.get("workoutTypeValueId"), _norm_text(w.get("title")))


def _anchor_warning(before: date_type | None, plan_after: dict[str, Any]) -> str | None:
    after = _anchor(plan_after)
    if before is not None and after != before:
        return (f"Week 1 moved: its Monday was {before.isoformat()} and is now "
                f"{after.isoformat() if after else 'undefined (plan is empty)'}. Every week "
                "number in this plan shifted — read the plan again before the next write.")
    return None


async def lo_plan_get_workouts(
    plan_id: int | str, weeks: list[int] | None = None, full: bool = False,
    save_to: str | None = None,
) -> dict[str, Any]:
    """Plan workouts with ids, laid out by week (1..) and day (1 = Monday).

    ``save_to`` writes the FULL rows (description + structured_workout) to a JSON file
    and keeps the reply brief (slim rows, or only per-week counts past MAX_BATCH rows),
    whatever ``full`` says.
    """
    pid = _plan_id(plan_id)
    if pid is None:
        return _err("VALIDATION_ERROR", "plan_id must be a positive integer.")
    wanted = {int(x) for x in weeks} if weeks else None
    async with TPClient() as client:
        plan, workouts, error = await _load(client, pid)
        if error or plan is None:
            return error or _err("API_ERROR", "Could not read the plan.")
        anchor = _anchor(plan)
        rows = [_row(w, anchor, full or bool(save_to)) for w in workouts]
        rows.sort(key=lambda r: (r["week"] or 0, r["day"] or 0, r["workout_id"] or 0))
        if wanted is not None:
            rows = [r for r in rows if r["week"] in wanted]
        out: dict[str, Any] = {"plan": _brief(plan), "count": len(rows),
                               "total_in_plan": len(workouts)}
        if save_to:
            target = Path(os.path.expanduser(save_to))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({**out, "workouts": rows}, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
            except OSError as e:
                return _err("VALIDATION_ERROR", f"save_to could not be written: {e}")
            out["saved_to"] = str(target)
            # FORK 2026-11-16: with save_to the reply never carries the full text (a
            # 131-workout read-back came back as 540 KB). Up to MAX_BATCH rows come back
            # slim; past that only the per-week counts — read the file for the rest.
            if len(rows) > MAX_BATCH:
                out["per_week"] = _per_week(rows)
                rows = []
                out["rows_omitted"] = f"more than {MAX_BATCH} rows: see saved_to"
            else:
                rows = [_slim(r) for r in rows]
        out["workouts"] = rows
        warning = _count_warning(plan, workouts)
        if warning:
            out["warnings"] = [warning]
        return out


async def lo_plan_create(
    title: str, description: str | None = None, language: str | None = "zh-Hant",
    allow_duplicate_title: bool = False,
) -> dict[str, Any]:
    """Create a new, private, unpriced plan."""
    name = (title or "").strip()
    if not name or len(name) > 100:
        return _err("VALIDATION_ERROR", "title must be 1–100 characters (TP limit).")
    async with TPClient() as client:
        listing = await client.get(_BASE)
        if listing.is_error:
            return _api_err(listing, "list plans")
        existing = [p for p in (listing.data or []) if isinstance(p, dict)]
        twin = next((p for p in existing if (p.get("title") or "").strip() == name), None)
        if twin is not None and not allow_duplicate_title:
            return _err("DUPLICATE_TITLE", f"A plan titled {name!r} already exists (plan_id "
                        f"{twin.get('planId')}). Pass allow_duplicate_title=true to create another.",
                        plan_id=twin.get("planId"))

        created = await client.post(_BASE, json={"title": name, "isDynamic": False})
        if created.is_error:
            return _api_err(created, "create plan")
        if not isinstance(created.data, dict) or not created.data.get("planId"):
            return _err("API_ERROR", "TrainingPeaks did not return the new plan.")
        pid = int(created.data["planId"])

        warnings: list[str] = []
        if description or language:
            plan, _, error = await _load(client, pid, with_workouts=False)
            if error or plan is None:
                warnings.append("Plan created, but it could not be read back to set description/language.")
            else:
                body = dict(plan)
                if description:
                    body["description"] = description
                if language:
                    body["planLanguage"] = language
                put = await client.put(f"{_BASE}/{pid}", json=body)
                if put.is_error:
                    warnings.append(f"Plan created, but description/language were not saved: {put.message}")

        plan, _, error = await _load(client, pid, with_workouts=False)
        if error or plan is None:
            return {"success": True, "plan_id": pid, "verified": False,
                    "warnings": warnings + ["The new plan could not be read back."]}
        verified = (
            (plan.get("title") or "").strip() == name
            and (not description or _norm_text(plan.get("description")) == _norm_text(description))
            and (not language or plan.get("planLanguage") == language)
            and not plan.get("isPublic")
        )
        out: dict[str, Any] = {"success": True, "verified": verified, "plan": _brief(plan),
                               "plan_id": pid, "language": plan.get("planLanguage"),
                               "price": plan.get("price")}
        if warnings:
            out["warnings"] = warnings
        return out


async def lo_plan_update_meta(
    plan_id: int | str, title: str | None = None, description: str | None = None,
    language: str | None = None, allow_public: bool = False,
) -> dict[str, Any]:
    """Change a plan's title / description / language. Nothing else is touched."""
    pid = _plan_id(plan_id)
    if pid is None:
        return _err("VALIDATION_ERROR", "plan_id must be a positive integer.")
    if title is None and description is None and language is None:
        return _err("VALIDATION_ERROR", "Nothing to change: give title, description or language.")
    name = title.strip() if title is not None else None
    if name is not None and (not name or len(name) > 100):
        return _err("VALIDATION_ERROR", "title must be 1–100 characters (TP limit).")
    async with TPClient() as client:
        plan, _, error = await _load(client, pid, with_workouts=False)
        if error or plan is None:
            return error or _err("API_ERROR", "Could not read the plan.")
        locked = _lock(plan, allow_public)
        if locked:
            return locked
        body = dict(plan)
        changed: dict[str, Any] = {}
        for key, new in (("title", name), ("description", description), ("planLanguage", language)):
            if new is not None and _norm_text(new) != _norm_text(plan.get(key)):
                changed[key] = {"before": plan.get(key), "after": new}
                body[key] = new
        if not changed:
            return {"success": True, "verified": True, "changed": {}, "plan": _brief(plan)}
        put = await client.put(f"{_BASE}/{pid}", json=body)
        if put.is_error:
            return _api_err(put, "update plan")
        after, _, error = await _load(client, pid, with_workouts=False)
        if error or after is None:
            return {"success": True, "verified": False, "changed": changed,
                    "warnings": ["Written, but the plan could not be read back."]}
        verified = all(_norm_text(after.get(k)) == _norm_text(v["after"]) for k, v in changed.items())
        untouched = all(after.get(k) == plan.get(k) for k in ("isPublic", "isSearchable", "price", "workoutCount"))
        return {"success": verified and untouched, "verified": verified,
                "store_fields_untouched": untouched, "changed": changed, "plan": _brief(after)}


async def lo_plan_add_workouts(
    plan_id: int | str, workouts: list[dict[str, Any]] | None = None, allow_public: bool = False,
    skip_if_exists: bool = True, dry_run: bool = False, payload_file: str | None = None,
) -> dict[str, Any]:
    """Create workouts inside a plan; every created workout is read back and compared.

    ``payload_file`` is a JSON file holding the workouts list (or ``{"workouts": [...]}``).
    FORK 2026-11-16: more than MAX_BATCH items are written in batches of MAX_BATCH,
    in week/day order; a batch with problems stops the rest.
    """
    if payload_file:
        if workouts:
            return _err("VALIDATION_ERROR", "Give workouts or payload_file, not both.")
        data, file_error = _read_json_file(payload_file)
        if file_error:
            return file_error
        workouts = data.get("workouts") if isinstance(data, dict) else data
    if not isinstance(workouts, list) or not workouts:
        return _err("VALIDATION_ERROR", "workouts must be a non-empty list.")
    if dry_run or len(workouts) <= MAX_BATCH:
        return await _add_batch(plan_id, workouts, allow_public, skip_if_exists, dry_run)

    # validate everything before the first write
    bad: list[dict[str, Any]] = []
    for index, raw in enumerate(workouts):
        try:
            _Item(**raw)
        except (ValidationError, TypeError) as e:
            msg = format_validation_error(e) if isinstance(e, ValidationError) else str(e)
            bad.append({"index": index, "error": msg})
    if bad:
        return _err("VALIDATION_ERROR", "Nothing was written: fix these items first.", errors=bad)
    order = sorted(range(len(workouts)), key=lambda i: (workouts[i]["week"], workouts[i]["day"], i))
    chunks = [order[i:i + MAX_BATCH] for i in range(0, len(order), MAX_BATCH)]
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    last: dict[str, Any] = {}
    stopped_at: int | None = None
    for n, chunk in enumerate(chunks):
        res = await _add_batch(plan_id, [workouts[i] for i in chunk], allow_public, skip_if_exists, False)
        if "rows" not in res:  # refused before writing (lock, read error, ...)
            if n == 0:
                return res
            warnings.append(f"Batch {n + 1} was refused: {res.get('message')}")
            stopped_at = n
            break
        last = res
        for row in res["rows"]:
            rows.append({**row, "index": chunk[row["index"]]})
        warnings.extend(res.get("warnings") or [])
        if not res.get("success"):
            stopped_at = n + 1
            break
    if stopped_at is not None:
        done = {r["index"] for r in rows}
        for i in order:
            if i not in done:
                w = workouts[i]
                rows.append({"index": i, "week": w["week"], "day": w["day"], "sport": w["sport"],
                             "title": w["title"], "status": "not_attempted"})
        warnings.append("Stopped after a batch with problems; later batches were not sent.")
    rows.sort(key=lambda r: r["index"])
    created = [r for r in rows if r["status"] == "created"]
    problems = [r for r in rows if r["status"] in ("failed", "uncertain", "not_attempted", "missing_after_create")]
    ok = not problems and all(r.get("verified") for r in created)
    out: dict[str, Any] = {
        "success": ok,
        "plan": last.get("plan"),
        "batches": len(chunks),
        "created": len(created),
        "skipped": sum(1 for r in rows if r["status"] == "skipped_exists"),
        "problems": len(problems),
        # more than MAX_BATCH rows: only what needs attention comes back
        "rows": [r for r in rows if not (r["status"] == "created" and r.get("verified"))],
        "created_ids": [r["workout_id"] for r in created],
    }
    if warnings:
        out["warnings"] = list(dict.fromkeys(warnings))
    return out


async def _add_batch(
    plan_id: int | str, workouts: list[dict[str, Any]], allow_public: bool,
    skip_if_exists: bool, dry_run: bool,
) -> dict[str, Any]:
    """One call's worth of creates (≤ MAX_BATCH unless dry_run)."""
    payload_file = None
    pid = _plan_id(plan_id)
    if pid is None:
        return _err("VALIDATION_ERROR", "plan_id must be a positive integer.")
    if payload_file:
        if workouts:
            return _err("VALIDATION_ERROR", "Give workouts or payload_file, not both.")
        data, file_error = _read_json_file(payload_file)
        if file_error:
            return file_error
        workouts = data.get("workouts") if isinstance(data, dict) else data
    if not isinstance(workouts, list) or not workouts:
        return _err("VALIDATION_ERROR", "workouts must be a non-empty list.")
    if len(workouts) > MAX_BATCH and not dry_run:
        return _err("VALIDATION_ERROR", f"At most {MAX_BATCH} workouts per batch (got {len(workouts)}).")

    items: list[_Item] = []
    bad: list[dict[str, Any]] = []
    for index, raw in enumerate(workouts):
        try:
            items.append(_Item(**raw))
        except ValidationError as e:
            bad.append({"index": index, "error": format_validation_error(e)})
        except TypeError as e:
            bad.append({"index": index, "error": str(e)})
    if bad:
        return _err("VALIDATION_ERROR", "Nothing was written: fix these items first.", errors=bad)

    async with TPClient() as client:
        plan, existing, error = await _load(client, pid)
        if error or plan is None:
            return error or _err("API_ERROR", "Could not read the plan.")
        locked = _lock(plan, allow_public)
        if locked:
            return locked
        plan_person_id = plan.get("planPersonId")
        if not plan_person_id:
            return _err("API_ERROR", "The plan has no planPersonId; cannot address its workouts.")

        anchor_before = _anchor(plan)
        anchor = anchor_before
        if anchor is None:
            if min(i.week for i in items) != 1:
                return _err("VALIDATION_ERROR", "The plan is empty, so this first batch must include "
                            "week 1: TP anchors week 1 on the earliest workout, and a batch that "
                            "starts later would become week 1 itself.")
            anchor = _monday(date_type.today())

        seen = {_dup_key(w) for w in existing}
        rows: list[dict[str, Any]] = []
        todo: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for index, item in enumerate(items):
            payload, perr = _payload(item, int(plan_person_id), _date_for(anchor, item.week, item.day))
            if perr or payload is None:
                return _err("VALIDATION_ERROR", f"Nothing was written: item {index}: {perr}")
            row: dict[str, Any] = {"index": index, "week": item.week, "day": item.day,
                                   "sport": item.sport, "title": item.title}
            key = _dup_key(payload)
            if skip_if_exists and key in seen:
                row["status"] = "skipped_exists"
            else:
                seen.add(key)
                row["status"] = "would_create" if dry_run else "pending"
                todo.append((row, payload))
            rows.append(row)

        if dry_run:
            return {"success": True, "dry_run": True, "plan": _brief(plan),
                    "would_create": len(todo), "skipped": len(rows) - len(todo), "rows": rows}

        failures_in_a_row = 0
        for row, payload in todo:
            if failures_in_a_row >= _ABORT_AFTER_FAILURES:
                row["status"] = "not_attempted"
                continue
            r = await client.post(f"{_BASE}/{pid}/workouts", json=payload)
            if not r.is_error and isinstance(r.data, dict) and r.data.get("workoutId"):
                row["status"] = "created"
                row["workout_id"] = r.data["workoutId"]
                failures_in_a_row = 0
                continue
            failures_in_a_row += 1
            code = r.error_code.value if r.error_code else "API_ERROR"
            row["status"] = "uncertain" if code in _UNKNOWN_OUTCOME else "failed"
            row["error"] = (r.message or "unexpected response")[:200]

        warnings: list[str] = []
        after_plan, after, error = await _load(client, pid)
        if error or after_plan is None:
            warnings.append("Workouts were sent but the plan could not be read back — nothing is verified.")
            after_plan, after = plan, []
        else:
            by_id = {w.get("workoutId"): w for w in after}
            known = {w.get("workoutId") for w in existing}
            fresh = {_dup_key(w): w for w in after if w.get("workoutId") not in known}
            for row, payload in todo:
                if row["status"] == "created":
                    got = by_id.get(row["workout_id"])
                    if got is None:
                        row["status"] = "missing_after_create"
                        continue
                    diff = _mismatch(payload, got)
                    row["verified"] = not diff
                    if diff:
                        row["mismatch"] = diff
                elif row["status"] == "uncertain":
                    got = fresh.get(_dup_key(payload))
                    if got is not None:  # the answer was lost but the write landed
                        row["status"] = "created"
                        row["workout_id"] = got.get("workoutId")
                        diff = _mismatch(payload, got)
                        row["verified"] = not diff
                        if diff:
                            row["mismatch"] = diff
            for w in (_count_warning(after_plan, after), _anchor_warning(anchor_before, after_plan)):
                if w:
                    warnings.append(w)

        created = [r for r in rows if r["status"] == "created"]
        problems = [r for r in rows if r["status"] in ("failed", "uncertain", "not_attempted", "missing_after_create")]
        out: dict[str, Any] = {
            "success": not problems and all(r.get("verified") for r in created),
            "plan": _brief(after_plan),
            "created": len(created),
            "skipped": sum(1 for r in rows if r["status"] == "skipped_exists"),
            "problems": len(problems),
            "rows": rows,
        }
        if warnings:
            out["warnings"] = warnings
        return out


async def lo_plan_update_workout(
    plan_id: int | str, workout_id: int | str, title: str | None = None,
    description: str | None = None, sport: str | None = None, week: int | None = None,
    day: int | None = None, duration_minutes: float | None = None, distance_km: float | None = None,
    tss_planned: float | None = None, if_planned: float | None = None,
    structure: dict[str, Any] | str | None = None, structured_workout: dict[str, Any] | None = None,
    clear_structure: bool = False, allow_public: bool = False,
) -> dict[str, Any]:
    """Change one plan workout. description="" clears the description."""
    pid = _plan_id(plan_id)
    wid = _plan_id(workout_id)
    if pid is None or wid is None:
        return _err("VALIDATION_ERROR", "plan_id and workout_id must be positive integers.")
    if (week is None) != (day is None):
        return _err("VALIDATION_ERROR", "Give week and day together to move a workout.")
    if week is not None and day is not None and not (1 <= week <= 104 and 1 <= day <= 7):
        return _err("VALIDATION_ERROR", "week must be 1–104 and day 1–7 (1 = Monday).")
    if sport is not None and sport not in SPORT_TYPE_MAP:
        return _err("VALIDATION_ERROR", f"unknown sport {sport!r}.")
    new_structure = structure is not None or structured_workout is not None
    if structure is not None and structured_workout is not None:
        return _err("VALIDATION_ERROR", "Give structure or structured_workout, not both.")
    if new_structure and clear_structure:
        return _err("VALIDATION_ERROR", "clear_structure cannot be combined with a new structure.")
    if new_structure and tss_planned is None:
        return _err("VALIDATION_ERROR", "A new structure needs tss_planned (hand-computed; this fork "
                    "never uploads an estimated TSS).")
    if title is not None and not title.strip():
        return _err("VALIDATION_ERROR", "title is blank.")
    given = (title, description, sport, week, duration_minutes, distance_km, tss_planned, if_planned)
    if all(v is None for v in given) and not new_structure and not clear_structure:
        return _err("VALIDATION_ERROR", "Nothing to change.")

    wire: str | None = None
    derived_minutes: float | None = None
    if new_structure:
        wire, derived_minutes, serr = _structure_string(structure, structured_workout)
        if serr:
            return _err("VALIDATION_ERROR", serr)

    async with TPClient() as client:
        plan, workouts, error = await _load(client, pid)
        if error or plan is None:
            return error or _err("API_ERROR", "Could not read the plan.")
        locked = _lock(plan, allow_public)
        if locked:
            return locked
        original = next((w for w in workouts if w.get("workoutId") == wid), None)
        if original is None:
            return _err("NOT_FOUND", f"Workout {wid} is not in plan {pid}.")
        anchor = _anchor(plan)

        # A partial PUT blanks whatever it omits (verified 2026-11-12), so the whole
        # read-back object goes up with only the requested fields replaced.
        body = dict(original)
        if title is not None:
            body["title"] = title.strip()
        if description is not None:
            body["description"] = description or None
        if sport is not None:
            body["workoutTypeFamilyId"], body["workoutTypeValueId"] = SPORT_TYPE_MAP[sport]
        if week is not None and day is not None and anchor is not None:
            body["workoutDay"] = f"{_date_for(anchor, week, day).isoformat()}T00:00:00"
        if duration_minutes is not None:
            body["totalTimePlanned"] = duration_minutes / 60.0
        elif derived_minutes is not None:
            body["totalTimePlanned"] = derived_minutes / 60.0
        if distance_km is not None:
            body["distancePlanned"] = distance_km * 1000
        if tss_planned is not None:
            body["tssPlanned"] = tss_planned
        if if_planned is not None:
            body["ifPlanned"] = if_planned
        if new_structure:
            body["structure"] = wire
        elif clear_structure:
            body["structure"] = None
        elif isinstance(original.get("structure"), dict):
            body["structure"] = json.dumps(original["structure"])

        put = await client.put(f"{_BASE}/{pid}/workouts/{wid}", json=body)
        if put.is_error:
            return _api_err(put, "update plan workout")

        after_plan, after, error = await _load(client, pid)
        if error or after_plan is None:
            return {"success": False, "verified": False, "workout_id": wid,
                    "warnings": ["Written, but the plan could not be read back."]}
        got = next((w for w in after if w.get("workoutId") == wid), None)
        if got is None:
            return _err("API_ERROR", f"Workout {wid} is missing from plan {pid} after the update.")
        diff = _mismatch(body, got)
        out: dict[str, Any] = {
            "success": not diff, "verified": not diff, "workout_id": wid,
            "before": _row(original, anchor, False), "after": _row(got, _anchor(after_plan), False),
            "plan": _brief(after_plan),
        }
        if diff:
            out["mismatch"] = diff
        warning = _anchor_warning(anchor, after_plan)
        if warning:
            out["warnings"] = [warning]
        return out


async def lo_plan_delete_workouts(
    plan_id: int | str, workout_ids: list[Any], allow_public: bool = False, dry_run: bool = False,
) -> dict[str, Any]:
    """Delete workouts from a plan; each is read first and confirmed gone afterwards.

    FORK 2026-11-16: more than MAX_BATCH ids are deleted in batches; the reply then lists
    only the rows that did not end as deleted.
    """
    ids = list(dict.fromkeys(workout_ids or []))
    if dry_run or len(ids) <= MAX_BATCH:
        return await _delete_batch(plan_id, ids, allow_public, dry_run)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    last: dict[str, Any] = {}
    for n in range(0, len(ids), MAX_BATCH):
        res = await _delete_batch(plan_id, ids[n:n + MAX_BATCH], allow_public, False)
        if "rows" not in res:
            if n == 0:
                return res
            warnings.append(f"A later batch was refused: {res.get('message')}")
            break
        last = res
        rows.extend(res["rows"])
        warnings.extend(res.get("warnings") or [])
        if not res.get("success"):
            warnings.append("Stopped after a batch with problems; later batches were not sent.")
            break
    done = {r["workout_id"] for r in rows}
    rows.extend({"workout_id": i, "status": "not_attempted"} for i in ids if i not in done)
    deleted = sum(1 for r in rows if r["status"] == "deleted")
    out: dict[str, Any] = {
        "success": all(r["status"] in ("deleted", "not_found") for r in rows),
        "plan": last.get("plan"),
        "deleted": deleted,
        "rows": [r for r in rows if r["status"] != "deleted"],
    }
    if warnings:
        out["warnings"] = list(dict.fromkeys(warnings))
    return out


async def _delete_batch(
    plan_id: int | str, workout_ids: list[Any], allow_public: bool, dry_run: bool,
) -> dict[str, Any]:
    pid = _plan_id(plan_id)
    if pid is None:
        return _err("VALIDATION_ERROR", "plan_id must be a positive integer.")
    ids = [_plan_id(x) for x in (workout_ids or [])]
    if not ids or any(i is None for i in ids):
        return _err("VALIDATION_ERROR", "workout_ids must be a non-empty list of workout ids.")
    if len(ids) > MAX_BATCH and not dry_run:
        return _err("VALIDATION_ERROR", f"At most {MAX_BATCH} workouts per batch.")

    async with TPClient() as client:
        plan, workouts, error = await _load(client, pid)
        if error or plan is None:
            return error or _err("API_ERROR", "Could not read the plan.")
        locked = _lock(plan, allow_public)
        if locked:
            return locked
        anchor = _anchor(plan)
        by_id = {w.get("workoutId"): w for w in workouts}
        rows: list[dict[str, Any]] = []
        for wid in dict.fromkeys(ids):
            found = by_id.get(wid)
            if found is None:
                rows.append({"workout_id": wid, "status": "not_found"})
            else:
                rows.append({**_row(found, anchor, False), "status": "would_delete" if dry_run else "pending"})
        if dry_run:
            return {"success": True, "dry_run": True, "plan": _brief(plan), "rows": rows}

        for row in rows:
            if row["status"] != "pending":
                continue
            r = await client.delete(f"{_BASE}/{pid}/workouts/{row['workout_id']}")
            if r.is_error:
                row["status"] = "failed"
                row["error"] = (r.message or "")[:200]
            else:
                row["status"] = "sent"

        warnings: list[str] = []
        after_plan, after, error = await _load(client, pid)
        if error or after_plan is None:
            warnings.append("Deletes were sent but the plan could not be read back — nothing is verified.")
            after_plan = plan
        else:
            left = {w.get("workoutId") for w in after}
            for row in rows:
                if row["status"] in ("sent", "failed"):
                    row["status"] = "still_present" if row["workout_id"] in left else "deleted"
            warning = _anchor_warning(anchor, after_plan)
            if warning:
                warnings.append(warning)
        out: dict[str, Any] = {
            "success": all(r["status"] in ("deleted", "not_found") for r in rows),
            "plan": _brief(after_plan),
            "deleted": sum(1 for r in rows if r["status"] == "deleted"),
            "rows": rows,
        }
        if warnings:
            out["warnings"] = warnings
        return out


async def lo_plan_replace_workouts(
    plan_id: int | str, payload_file: str, backup_to: str | None = None,
    allow_public: bool = False, dry_run: bool = False,
) -> dict[str, Any]:
    """Swap a plan's whole content for the payload file (FORK 2026-11-16).

    Order: back up every current workout to a file → add the new set (same-title checks
    off, so an old workout with the same day/sport/title does not block its new version)
    → only when every new workout was created and read back equal, delete the old set.
    If the add step has any problem nothing old is deleted.
    """
    pid = _plan_id(plan_id)
    if pid is None:
        return _err("VALIDATION_ERROR", "plan_id must be a positive integer.")
    data, file_error = _read_json_file(payload_file)
    if file_error:
        return file_error
    workouts = data.get("workouts") if isinstance(data, dict) else data
    if not isinstance(workouts, list) or not workouts:
        return _err("VALIDATION_ERROR", "payload_file must hold a non-empty workouts list.")

    async with TPClient() as client:
        plan, existing, error = await _load(client, pid)
        if error or plan is None:
            return error or _err("API_ERROR", "Could not read the plan.")
        locked = _lock(plan, allow_public)
        if locked:
            return locked
    anchor = _anchor(plan)
    old_rows = [_row(w, anchor, True) for w in existing]
    old_ids = [r["workout_id"] for r in old_rows]
    target = Path(os.path.expanduser(backup_to or f"{payload_file}.backup-{pid}.json"))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"plan": _brief(plan), "workouts": old_rows}, ensure_ascii=False,
                                     indent=1), encoding="utf-8")
    except OSError as e:
        return _err("VALIDATION_ERROR", f"backup_to could not be written, nothing was changed: {e}")
    base: dict[str, Any] = {"plan_before": _brief(plan), "old_count": len(old_ids),
                            "new_count": len(workouts), "backup_to": str(target)}
    if dry_run:
        check = await lo_plan_add_workouts(pid, workouts, allow_public=allow_public,
                                           skip_if_exists=False, dry_run=True)
        if "rows" not in check:
            return {**check, **base}
        return {"success": True, "dry_run": True, **base,
                "would_create": check.get("would_create"), "would_delete": len(old_ids)}

    added = await lo_plan_add_workouts(pid, workouts, allow_public=allow_public, skip_if_exists=False)
    if not added.get("success"):
        return {"success": False, "phase": "add", **base,
                "message": "The new set did not land cleanly, so the old workouts were NOT deleted. "
                           "Fix the rows below, then delete the new ids (created_ids) or retry.",
                "created_ids": added.get("created_ids") or [r.get("workout_id") for r in added.get("rows", [])
                                                           if r.get("status") == "created"],
                "add": {k: v for k, v in added.items() if k != "plan"}}

    removed = await lo_plan_delete_workouts(pid, old_ids, allow_public=allow_public) if old_ids else \
        {"success": True, "deleted": 0, "rows": []}
    async with TPClient() as client:
        after_plan, after, error = await _load(client, pid)
    out: dict[str, Any] = {
        "success": bool(removed.get("success")) and not error and len(after) == len(workouts),
        **base,
        "created": added.get("created"),
        "deleted": removed.get("deleted"),
        "plan": _brief(after_plan) if after_plan else None,
        "count_after": len(after),
    }
    warnings = list(added.get("warnings") or []) + list(removed.get("warnings") or [])
    if not removed.get("success"):
        out["delete_problems"] = [r for r in removed.get("rows", []) if r.get("status") != "deleted"]
    if after_plan is not None:
        w = _anchor_warning(anchor, after_plan)
        if w:
            warnings.append(w)
    if len(after) != len(workouts):
        warnings.append(f"The plan now holds {len(after)} workouts; the payload had {len(workouts)}.")
    if warnings:
        out["warnings"] = list(dict.fromkeys(warnings))
    return out


def register_lo_plans(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    plan_id = {"type": "integer", "description": "Plan id (from tp_list_training_plans)."}
    allow_public = {
        "type": "boolean", "default": False,
        "description": "Required to write to a PUBLISHED plan. Only after the coach approved "
                       "this specific change in chat.",
    }
    workout_fields = {
        "sport": {"type": "string", "description": "Swim, Bike, Run, Brick, Strength, DayOff, Other, ..."},
        "title": {"type": "string"},
        "duration_minutes": {"type": "number"},
        "description": {"type": "string"},
        "distance_km": {"type": "number"},
        "tss_planned": {"type": "number", "description": "Required with structure / structured_workout."},
        "if_planned": {"type": "number"},
        "structure": {"type": ["object", "string"], "description": "Simplified interval structure."},
        "structured_workout": {"type": "object", "description": "Native TP structure (tp_build.py output)."},
    }
    week = {"type": "integer", "description": "Week number, 1 = first week of the plan."}
    day = {"type": "integer", "description": "1 = Monday … 7 = Sunday."}

    tools.append(Tool(
        name="lo_plan_get_workouts",
        description=(
            "Workouts of a training plan with their workout_id, week (1..) and day (1 = Monday), "
            "sport, title, duration, TSS, has_structure and the first characters of the description. "
            "weeks=[1,2] limits the output; full=true adds the whole description and the "
            "structured_workout. save_to writes those full rows to a JSON file on this machine "
            f"and keeps the reply brief whatever full says (slim rows; past {MAX_BATCH} rows only "
            "per-week counts): use it to compare what TP stored with the payload file by script "
            "instead of pasting text back. Read-only."
        ),
        input_schema={"type": "object", "properties": {
            "plan_id": plan_id,
            "weeks": {"type": "array", "items": {"type": "integer"}},
            "full": {"type": "boolean", "default": False},
            "save_to": {"type": "string", "description": "Absolute path (on the machine running "
                        "this server) for the full read-back as JSON."},
        }, "required": ["plan_id"]},
    ))
    tools.append(Tool(
        name="lo_plan_create",
        description=(
            "Create a new training plan: private, unpriced, not listed in the store. Refuses a "
            "title that already exists. Price, category, sample weeks and publishing are set by "
            "the coach in the TP web UI. Fill it with lo_plan_add_workouts (week 1 first)."
        ),
        input_schema={"type": "object", "properties": {
            "title": {"type": "string", "description": "1–100 characters."},
            "description": {"type": "string"},
            "language": {"type": "string", "default": "zh-Hant"},
            "allow_duplicate_title": {"type": "boolean", "default": False},
        }, "required": ["title"]},
    ))
    tools.append(Tool(
        name="lo_plan_update_meta",
        description=(
            "Change a plan's title, description or language (read, merge, write, read back). "
            "Never touches price, isPublic or the store listing. Published plans need allow_public."
        ),
        input_schema={"type": "object", "properties": {
            "plan_id": plan_id, "title": {"type": "string"}, "description": {"type": "string"},
            "language": {"type": "string"}, "allow_public": allow_public,
        }, "required": ["plan_id"]},
    ))
    tools.append(Tool(
        name="lo_plan_add_workouts",
        description=(
            f"Create workouts inside a training plan (more than {MAX_BATCH} are sent in batches "
            f"of {MAX_BATCH}, in week/day order; a batch with problems stops the rest), addressed "
            "by week and day "
            "(1 = Monday). Same fields as tp_create_workouts_batch items, with week/day instead of "
            "date. Every created workout is read back and compared; rows report created / "
            "skipped_exists / failed / uncertain. An empty plan must receive week 1 in its first "
            "batch. Not retried automatically. dry_run lists what would be created. Published "
            "plans need allow_public. payload_file reads the workouts from a JSON file (a list, or "
            "{\"workouts\": [...]}) instead of the workouts argument: prefer it for anything with "
            "long text, so nothing is re-typed."
        ),
        input_schema={"type": "object", "properties": {
            "plan_id": plan_id,
            "workouts": {"type": "array", "items": {
                "type": "object", "properties": {"week": week, "day": day, **workout_fields},
                "required": ["week", "day", "sport", "title"]}},
            "skip_if_exists": {"type": "boolean", "default": True,
                               "description": "Skip an item when the same day + sport + title is already there."},
            "dry_run": {"type": "boolean", "default": False},
            "allow_public": allow_public,
            "payload_file": {"type": "string", "description": "Absolute path (on the machine "
                             "running this server) of a JSON file with the workouts. Use instead "
                             "of workouts."},
        }, "required": ["plan_id"]},
    ))
    tools.append(Tool(
        name="lo_plan_update_workout",
        description=(
            "Change one workout of a training plan: only the given fields change, the rest is "
            "re-sent as read (a partial write would blank them). week+day move it; description=\"\" "
            "clears the description; a new structure needs tss_planned. Read back and compared. "
            "Published plans need allow_public. payload_file reads the fields from a JSON object "
            "in a file (same keys as the arguments; arguments given directly win)."
        ),
        input_schema={"type": "object", "properties": {
            "plan_id": plan_id, "workout_id": {"type": "integer"}, "week": week, "day": day,
            **workout_fields, "clear_structure": {"type": "boolean", "default": False},
            "allow_public": allow_public,
            "payload_file": {"type": "string", "description": "Absolute path of a JSON object "
                             "with the fields to change."},
        }, "required": ["plan_id", "workout_id"]},
    ))
    tools.append(Tool(
        name="lo_plan_delete_workouts",
        description=(
            "Delete workouts from a training plan. Each is read first (week/day/sport/title are "
            f"reported), deleted, then confirmed gone. More than {MAX_BATCH} ids go in batches and "
            "the reply then lists only rows that did not end deleted. dry_run lists what would be deleted. "
            "Irreversible — only after the coach approved it. Published plans need allow_public."
        ),
        input_schema={"type": "object", "properties": {
            "plan_id": plan_id,
            "workout_ids": {"type": "array", "items": {"type": "integer"}},
            "dry_run": {"type": "boolean", "default": False},
            "allow_public": allow_public,
        }, "required": ["plan_id", "workout_ids"]},
    ))

    tools.append(Tool(
        name="lo_plan_replace_workouts",
        description=(
            "Replace ALL workouts of a training plan with the workouts in payload_file (same "
            "item shape as lo_plan_add_workouts). Backs the current workouts up to backup_to "
            "(default: next to the payload file), adds the new set in batches with the "
            "same-title skip turned off, and deletes the old set only when every new workout "
            "was created and read back equal; otherwise nothing old is deleted and the reply "
            "lists created_ids. dry_run only counts. Irreversible delete step — only after the "
            "coach approved this replacement. Published plans need allow_public."
        ),
        input_schema={"type": "object", "properties": {
            "plan_id": plan_id,
            "payload_file": {"type": "string", "description": "Absolute path (on the machine "
                             "running this server) of the new workouts JSON (list or {\"workouts\": [...]})."},
            "backup_to": {"type": "string", "description": "Absolute path for the backup of the "
                          "current workouts (full rows). Default: <payload_file>.backup-<plan_id>.json"},
            "dry_run": {"type": "boolean", "default": False},
            "allow_public": allow_public,
        }, "required": ["plan_id", "payload_file"]},
    ))


    async def _h_get(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_plan_get_workouts(plan_id=args["plan_id"], weeks=args.get("weeks"),
                                          full=bool(args.get("full", False)),
                                          save_to=args.get("save_to"))

    async def _h_create(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_plan_create(title=args["title"], description=args.get("description"),
                                    language=args.get("language", "zh-Hant"),
                                    allow_duplicate_title=bool(args.get("allow_duplicate_title", False)))

    async def _h_meta(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_plan_update_meta(plan_id=args["plan_id"], title=args.get("title"),
                                         description=args.get("description"), language=args.get("language"),
                                         allow_public=bool(args.get("allow_public", False)))

    async def _h_add(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_plan_add_workouts(plan_id=args["plan_id"], workouts=args.get("workouts") or None,
                                          allow_public=bool(args.get("allow_public", False)),
                                          skip_if_exists=bool(args.get("skip_if_exists", True)),
                                          dry_run=bool(args.get("dry_run", False)),
                                          payload_file=args.get("payload_file"))

    async def _h_update(args: dict[str, Any]) -> dict[str, Any]:
        keys = ("title", "description", "sport", "week", "day", "duration_minutes", "distance_km",
                "tss_planned", "if_planned", "structure", "structured_workout")
        fields: dict[str, Any] = {}
        if args.get("payload_file"):
            data, file_error = _read_json_file(args["payload_file"])
            if file_error:
                return file_error
            if not isinstance(data, dict):
                return _err("VALIDATION_ERROR", "payload_file must hold one JSON object.")
            unknown = sorted(set(data) - set(keys))
            if unknown:
                return _err("VALIDATION_ERROR", f"payload_file has unknown keys: {unknown}")
            fields.update({k: v for k, v in data.items() if v is not None})
        fields.update({k: args[k] for k in keys if args.get(k) is not None})
        return await lo_plan_update_workout(
            plan_id=args["plan_id"], workout_id=args["workout_id"],
            clear_structure=bool(args.get("clear_structure", False)),
            allow_public=bool(args.get("allow_public", False)), **fields)

    async def _h_delete(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_plan_delete_workouts(plan_id=args["plan_id"], workout_ids=args.get("workout_ids") or [],
                                             allow_public=bool(args.get("allow_public", False)),
                                             dry_run=bool(args.get("dry_run", False)))

    handlers["lo_plan_get_workouts"] = _h_get
    handlers["lo_plan_create"] = _h_create
    handlers["lo_plan_update_meta"] = _h_meta
    handlers["lo_plan_add_workouts"] = _h_add
    handlers["lo_plan_update_workout"] = _h_update
    handlers["lo_plan_delete_workouts"] = _h_delete

    async def _h_replace(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_plan_replace_workouts(plan_id=args["plan_id"], payload_file=args["payload_file"],
                                              backup_to=args.get("backup_to"),
                                              allow_public=bool(args.get("allow_public", False)),
                                              dry_run=bool(args.get("dry_run", False)))

    handlers["lo_plan_replace_workouts"] = _h_replace
