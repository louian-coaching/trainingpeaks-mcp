"""Strength Builder workouts inside a training plan (FORK 2026-11-18, 教練指示).

  lo_plan_get_strength_workouts — the plan's Strength Builder workouts with ids and week/day
  lo_plan_add_strength_workouts — create Strength Builder workouts in a plan, each read back

lo_plans.py writes ordinary workouts through /plans/v1. Strength Builder workouts live on
the Peaksware strength API instead and never show up in /plans/v1/plans/{id}/workouts
(though plan.workoutCount counts them), so the plan tools could neither see nor create them.

Read live on 2026-11-18 (plan 538631, read-only):
  * A plan's Strength Builder workout is an ordinary strength document whose
    ``calendarId`` is the plan's ``planPersonId`` and whose ``prescribedDate`` is a date
    inside the plan. Week/day therefore map exactly as in lo_plans (week 1 = the
    Monday-week of plan.startDate).
  * GET /rx/activity/v1/plans/{planId}/workouts/{start}/{end} → bare array of summaries
    (id, prescribedDate, calendarId, title, instructions, totalSets, totalBlocks, ...).
  * Detail / update / delete are the generic strength routes, so
    tp_get_strength_workout, lo_update_strength_verified and tp_delete_strength_workout
    work on these ids as they are (that is how the 14 Sub30 sessions were rewritten on
    2026-11-16).

Creating is the same UPSERT the calendar tool uses — POST /rx/activity/v1/workouts/save —
with ``calendarId`` = planPersonId. Every created workout is read back (detail GET) and
compared: fingerprint (title, instructions, every exercise's sets), date and calendarId.

Guards (same as lo_plans): published plans need ``allow_public``; nothing is retried; a
write with no answer is looked up in the plan's strength list, never sent twice.

Coaching checks: tp_create_strength_workout's lint runs on every item. WarmUp-first
(FMT-52) blocks the write unless ``force``. A loaded exercise without a weight only warns
here: a plan is sold to people whose strength is unknown, so plans carry no weights
(教練 2026-11-16 選單裁示, the FMT-51 exception for plans).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date as date_type
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from tp_mcp.client import TPClient
from tp_mcp.tools import lo_plans as _p
from tp_mcp.tools import strength as _s
from tp_mcp.tools.lo_strength import _diff_fp, fingerprint

logger = logging.getLogger("tp-mcp")

MAX_ITEMS = 40
_ABORT_AFTER_FAILURES = 3
_HEAD_CHARS = 60
_WORKOUTS = f"{_s.STRENGTH_API_BASE}/rx/activity/v1/workouts"
_PLANS = f"{_s.STRENGTH_API_BASE}/rx/activity/v1/plans"


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message, **extra}


async def _token(client: TPClient) -> tuple[str | None, dict[str, Any] | None]:
    """Bearer token for the strength API (no athlete is involved: plans belong to the coach)."""
    token = await client._ensure_access_token()
    if not token.success:
        return None, _err("AUTH_INVALID", token.message or "Failed to obtain access token.")
    access = client._token_cache.access_token
    if not access:
        return None, _err("AUTH_INVALID", "No access token available. Re-authenticate.")
    return access, None


def _span(plan: dict[str, Any], anchor: date_type, last: date_type | None = None) -> tuple[str, str]:
    lo = anchor - timedelta(days=7)
    hi = anchor + timedelta(days=int(plan.get("dayCount") or 0) + 21)
    if last is not None and last + timedelta(days=7) > hi:
        hi = last + timedelta(days=7)
    return lo.isoformat(), hi.isoformat()


async def _list(h: httpx.AsyncClient, access: str, plan_id: int, lo: str, hi: str,
                ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    r = await h.get(f"{_PLANS}/{plan_id}/workouts/{lo}/{hi}", headers=_s._headers(access))
    if r.status_code != 200:
        return [], _s._map_status(r.status_code, r.text)
    items = r.json()
    if isinstance(items, dict):
        items = items.get("data") or []
    return [w for w in items if isinstance(w, dict)], None


def _key(day: Any, title: Any) -> tuple[str, str]:
    return str(day or "")[:10], str(title or "").strip()


def _sets_in(prescription: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for s in prescription.get("sets") or []:
        out.append({pv.get("parameter"): pv.get("prescribedValue") for pv in s.get("parameterValues") or []
                    if pv.get("parameter") and pv.get("prescribedValue") is not None})
    return out


def _blocks_in(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """A stored document's blocks in the INPUT shape, so a read-back can be a payload."""
    blocks = []
    for b in doc.get("blocks") or []:
        exercises = []
        for p in b.get("prescriptions") or []:
            ex = p.get("exercise") or {}
            exercises.append({"id": str(ex.get("id")), "exercise": ex.get("title"),
                              "notes": p.get("coachNotes"), "sets": _sets_in(p)})
        blocks.append({"type": b.get("blockType"), "title": b.get("title"),
                       "notes": b.get("coachNotes"), "exercises": exercises})
    return blocks


def _row(w: dict[str, Any], anchor: date_type | None) -> dict[str, Any]:
    week, day = _p._week_day(anchor, _p._iso_day(w.get("prescribedDate")))
    snap = w.get("snapshot") or {}
    return {
        "workout_id": str(w.get("id")),
        "week": week,
        "day": day,
        "title": (w.get("title") or "").strip(),
        "total_blocks": w.get("totalBlocks", snap.get("totalBlocks")),
        "total_sets": w.get("totalSets", snap.get("totalSets")),
    }


async def lo_plan_get_strength_workouts(
    plan_id: int | str, weeks: list[int] | None = None, full: bool = False,
    save_to: str | None = None,
) -> dict[str, Any]:
    """Strength Builder workouts of a plan, laid out by week (1..) and day (1 = Monday).

    ``full`` (or ``save_to``) reads every workout's detail and adds ``instructions`` and
    ``blocks`` in the same shape lo_plan_add_strength_workouts takes, so a read-back can
    be compared with a payload file or turned into one. ``save_to`` writes those full
    rows to a JSON file and keeps the reply brief.
    """
    pid = _p._plan_id(plan_id)
    if pid is None:
        return _err("VALIDATION_ERROR", "plan_id must be a positive integer.")
    wanted = {int(x) for x in weeks} if weeks else None
    async with TPClient() as client:
        plan, _, error = await _p._load(client, pid, with_workouts=False)
        if error or plan is None:
            return error or _err("API_ERROR", "Could not read the plan.")
        anchor = _p._anchor(plan)
        out: dict[str, Any] = {"plan": _p._brief(plan), "count": 0, "workouts": []}
        if anchor is None:
            return out
        access, terr = await _token(client)
        if terr or access is None:
            return terr or _err("AUTH_INVALID", "No access token available.")
        try:
            async with httpx.AsyncClient(timeout=_s.STRENGTH_TIMEOUT) as h:
                items, lerr = await _list(h, access, pid, *_span(plan, anchor))
                if lerr:
                    return lerr
                rows = []
                for w in items:
                    row = _row(w, anchor)
                    if wanted is not None and row["week"] not in wanted:
                        continue
                    if full or save_to:
                        r = await h.get(f"{_WORKOUTS}/{row['workout_id']}", headers=_s._headers(access))
                        if r.status_code != 200:
                            return _s._map_status(r.status_code, r.text)
                        doc = r.json().get("data") or {}
                        row["instructions"] = doc.get("instructions")
                        row["blocks"] = _blocks_in(doc)
                    else:
                        row["instructions_head"] = (w.get("instructions") or "")[:_HEAD_CHARS] or None
                    rows.append(row)
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Strength read timed out.")
        except httpx.RequestError:
            logger.exception("Network error reading plan strength workouts")
            return _err("NETWORK_ERROR", "A network error occurred.")
    rows.sort(key=lambda r: (r["week"] or 0, r["day"] or 0, r["workout_id"]))
    out["count"] = len(rows)
    if save_to:
        target = Path(os.path.expanduser(save_to))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({**out, "workouts": rows}, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        except OSError as e:
            return _err("VALIDATION_ERROR", f"save_to could not be written: {e}")
        out["saved_to"] = str(target)
        rows = [{k: v for k, v in r.items() if k not in ("instructions", "blocks")} for r in rows]
    out["workouts"] = rows
    return out


def _check_item(index: int, raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict):
        return None, "must be an object"
    unknown = sorted(set(raw) - {"week", "day", "title", "blocks", "instructions", "mobility"})
    if unknown:
        return None, f"unknown keys {unknown}"
    try:
        week, day = int(raw.get("week")), int(raw.get("day"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, "week and day must be integers"
    if not 1 <= week <= 104 or not 1 <= day <= 7:
        return None, "week must be 1..104 and day 1..7 (1 = Monday)"
    title = str(raw.get("title") or "").strip()
    if not title:
        return None, "title is required"
    blocks = raw.get("blocks")
    if not isinstance(blocks, list):
        return None, "blocks must be a list"
    invalid = _s._validate_blocks(blocks)
    if invalid:
        return None, invalid
    instructions = raw.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        return None, "instructions must be text"
    return {"index": index, "week": week, "day": day, "title": title, "blocks": blocks,
            "instructions": instructions, "mobility": bool(raw.get("mobility", False))}, None


def _lint(item: dict[str, Any]) -> dict[str, Any]:
    lint = _s._lint_blocks(item["blocks"], title=item["title"], mobility=item["mobility"])
    errors = [e for e in lint["errors"] if "FMT-51" not in e]
    no_weight = [e for e in lint["errors"] if "FMT-51" in e]
    warnings = list(lint["warnings"])
    if no_weight:
        warnings.append(f"{len(no_weight)} loaded exercise(s) carry no weight — allowed in a plan; "
                        "say how to choose the load in the notes")
    return {"errors": errors, "warnings": warnings,
            "exercises": [f"{r['title']} ({r['id']})" for r in lint["resolved"]]}


async def lo_plan_add_strength_workouts(
    plan_id: int | str, workouts: list[dict[str, Any]] | None = None, allow_public: bool = False,
    skip_if_exists: bool = True, force: bool = False, dry_run: bool = False,
    payload_file: str | None = None,
) -> dict[str, Any]:
    """Create Strength Builder workouts inside a plan; each one is read back and compared."""
    pid = _p._plan_id(plan_id)
    if pid is None:
        return _err("VALIDATION_ERROR", "plan_id must be a positive integer.")
    if payload_file:
        if workouts:
            return _err("VALIDATION_ERROR", "Give workouts or payload_file, not both.")
        data, file_error = _p._read_json_file(payload_file)
        if file_error:
            return file_error
        workouts = data.get("workouts") if isinstance(data, dict) else data
    if not isinstance(workouts, list) or not workouts:
        return _err("VALIDATION_ERROR", "workouts must be a non-empty list.")
    if len(workouts) > MAX_ITEMS and not dry_run:
        return _err("VALIDATION_ERROR", f"At most {MAX_ITEMS} strength workouts per call "
                    f"(got {len(workouts)}); send the rest in a second call.")

    items: list[dict[str, Any]] = []
    bad: list[dict[str, Any]] = []
    for index, raw in enumerate(workouts):
        item, problem = _check_item(index, raw)
        if problem or item is None:
            bad.append({"index": index, "error": problem})
        else:
            items.append(item)
    if bad:
        return _err("VALIDATION_ERROR", "Nothing was written: fix these items first.", errors=bad)
    lints = {item["index"]: _lint(item) for item in items}
    failed_lint = [{"index": i, "title": items[i]["title"], "errors": lint["errors"]}
                   for i, lint in lints.items() if lint["errors"]]
    if failed_lint and not force:
        return _err("LINT_FAILED", "Nothing was written: these items failed coaching checks "
                    "(pass force=true for a deliberate exception).", errors=failed_lint)

    async with TPClient() as client:
        plan, _, error = await _p._load(client, pid, with_workouts=False)
        if error or plan is None:
            return error or _err("API_ERROR", "Could not read the plan.")
        locked = _p._lock(plan, allow_public)
        if locked:
            return locked
        plan_person_id = plan.get("planPersonId")
        if not plan_person_id:
            return _err("API_ERROR", "The plan has no planPersonId; cannot address its workouts.")
        anchor = _p._anchor(plan)
        if anchor is None:
            return _err("VALIDATION_ERROR", "The plan is empty. Put its week-1 workouts in first with "
                        "lo_plan_add_workouts: week numbers are anchored on the plan's earliest "
                        "workout, and a strength workout alone has not been shown to set that anchor.")
        access, terr = await _token(client)
        if terr or access is None:
            return terr or _err("AUTH_INVALID", "No access token available.")

        order = sorted(items, key=lambda it: (it["week"], it["day"], it["index"]))
        dates = {it["index"]: _p._date_for(anchor, it["week"], it["day"]) for it in items}
        lo, hi = _span(plan, anchor, max(dates.values()))
        warnings: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=_s.STRENGTH_TIMEOUT) as h:
                existing, lerr = await _list(h, access, pid, lo, hi)
                if lerr:
                    return lerr
                seen = {_key(w.get("prescribedDate"), w.get("title")) for w in existing}
                rows: dict[int, dict[str, Any]] = {}
                todo: list[tuple[dict[str, Any], dict[str, Any]]] = []
                for it in order:
                    lint = lints[it["index"]]
                    row: dict[str, Any] = {"index": it["index"], "week": it["week"], "day": it["day"],
                                           "title": it["title"]}
                    if lint["warnings"]:
                        row["lint_warnings"] = lint["warnings"]
                    if lint["errors"]:
                        row["forced_past"] = lint["errors"]
                    key = _key(dates[it["index"]].isoformat(), it["title"])
                    if skip_if_exists and key in seen:
                        row["status"] = "skipped_exists"
                    else:
                        seen.add(key)
                        row["status"] = "would_create" if dry_run else "pending"
                        payload = _s._build_payload(int(plan_person_id), dates[it["index"]].isoformat(),
                                                    it["title"], it["blocks"], it["instructions"])
                        todo.append((row, payload))
                    if dry_run:
                        row["exercises"] = lint["exercises"]
                    rows[it["index"]] = row
                listing = [rows[i] for i in sorted(rows)]
                if dry_run:
                    return {"success": True, "dry_run": True, "plan": _p._brief(plan),
                            "would_create": len(todo), "skipped": len(rows) - len(todo), "rows": listing}

                failures_in_a_row = 0
                for row, payload in todo:
                    if failures_in_a_row >= _ABORT_AFTER_FAILURES:
                        row["status"] = "not_attempted"
                        continue
                    try:
                        r = await h.post(f"{_WORKOUTS}/save", headers=_s._headers(access), json=payload)
                    except httpx.RequestError:  # includes timeouts: the write may have landed
                        failures_in_a_row += 1
                        row["status"] = "uncertain"
                        row["error"] = "no answer from the strength API"
                        continue
                    new_id = None
                    if r.status_code == 200:
                        new_id = (r.json().get("data") or {}).get("id")
                    if new_id:
                        row["status"] = "created"
                        row["workout_id"] = str(new_id)
                        failures_in_a_row = 0
                        continue
                    failures_in_a_row += 1
                    row["status"] = "failed"
                    try:
                        detail = r.json().get("errors")
                    except Exception:  # noqa: BLE001
                        detail = None
                    row["error"] = str(detail or f"HTTP {r.status_code} {r.text[:160]}")[:300]

                # read back: find writes whose answer was lost, then compare every created one
                after, lerr = await _list(h, access, pid, lo, hi)
                if lerr:
                    warnings.append("Workouts were sent but the plan's strength list could not be "
                                    "read back — lost answers were not resolved.")
                    after = []
                known = {str(w.get("id")) for w in existing}
                fresh = {_key(w.get("prescribedDate"), w.get("title")): w for w in after
                         if str(w.get("id")) not in known}
                for row, payload in todo:
                    if row["status"] == "uncertain":
                        got = fresh.get(_key(payload["prescribedDate"], payload["title"]))
                        if got is None:
                            continue
                        row["status"] = "created"
                        row["workout_id"] = str(got.get("id"))
                        row.pop("error", None)
                    if row["status"] != "created":
                        continue
                    r = await h.get(f"{_WORKOUTS}/{row['workout_id']}", headers=_s._headers(access))
                    doc = (r.json().get("data") or {}) if r.status_code == 200 else {}
                    if not doc:
                        row["status"] = "missing_after_create"
                        continue
                    diff = _diff_fp(fingerprint(_s._fmt_workout_detail(payload)),
                                    fingerprint(_s._fmt_workout_detail(doc)))
                    if str(doc.get("prescribedDate") or "")[:10] != payload["prescribedDate"]:
                        diff.append(f"date: expected {payload['prescribedDate']}, landed {doc.get('prescribedDate')}")
                    if str(doc.get("calendarId")) != str(plan_person_id):
                        diff.append(f"calendarId: expected {plan_person_id}, landed {doc.get('calendarId')}")
                    row["verified"] = not diff
                    if diff:
                        row["mismatch"] = diff
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Strength API timed out before anything was written.")
        except httpx.RequestError:
            logger.exception("Network error in lo_plan_add_strength_workouts")
            return _err("NETWORK_ERROR", "A network error occurred.")

        after_plan, _, error = await _p._load(client, pid, with_workouts=False)
        if error or after_plan is None:
            after_plan = plan
            warnings.append("The plan could not be read again after the writes.")
        else:
            moved = _p._anchor_warning(anchor, after_plan)
            if moved:
                warnings.append(moved)

    created = [r for r in listing if r["status"] == "created"]
    problems = [r for r in listing
                if r["status"] in ("failed", "uncertain", "not_attempted", "missing_after_create")]
    out: dict[str, Any] = {
        "success": not problems and all(r.get("verified") for r in created),
        "plan": _p._brief(after_plan),
        "created": len(created),
        "skipped": sum(1 for r in listing if r["status"] == "skipped_exists"),
        "problems": len(problems),
        "rows": listing,
    }
    if warnings:
        out["warnings"] = warnings
    return out


def register_lo_plan_strength(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    plan_id = {"type": "integer", "description": "Plan id (from tp_list_training_plans)."}
    tools.append(Tool(
        name="lo_plan_get_strength_workouts",
        description=(
            "Strength Builder workouts of a training plan (lo_plan_get_workouts never returns them): "
            "workout_id, week, day (1 = Monday), title, block and set counts. full=true adds "
            "instructions and blocks in the shape lo_plan_add_strength_workouts takes; save_to writes "
            "those full rows to a JSON file and keeps the reply brief. Change one with "
            "lo_update_strength_verified, delete with tp_delete_strength_workout. Read-only."
        ),
        input_schema={"type": "object", "properties": {
            "plan_id": plan_id,
            "weeks": {"type": "array", "items": {"type": "integer"}},
            "full": {"type": "boolean", "default": False},
            "save_to": {"type": "string", "description": "Absolute path on this server's machine."},
        }, "required": ["plan_id"]},
    ))
    tools.append(Tool(
        name="lo_plan_add_strength_workouts",
        description=(
            f"Create Strength Builder workouts inside a training plan (at most {MAX_ITEMS} per call). "
            "Each item: week, day (1 = Monday), title, blocks (same as tp_create_strength_workout; "
            "ids from tp_search_exercises), optional instructions and mobility. Every created workout "
            "is read back and compared; rows report created / skipped_exists / failed / uncertain and "
            "verified. The first block must be a WarmUp unless mobility or force; a loaded exercise "
            "without a weight only warns (plans carry no weights). The plan must already hold its "
            "week-1 workouts. Never retried. dry_run resolves exercise names and writes nothing. "
            "Published plans need allow_public. Prefer payload_file (a JSON list, or "
            "{\"workouts\": [...]}) so long notes are not re-typed."
        ),
        input_schema={"type": "object", "properties": {
            "plan_id": plan_id,
            "workouts": {"type": "array", "items": {"type": "object"}},
            "skip_if_exists": {"type": "boolean", "default": True,
                               "description": "Skip when the same day + title is already there."},
            "force": {"type": "boolean", "default": False},
            "dry_run": {"type": "boolean", "default": False},
            "allow_public": {"type": "boolean", "default": False,
                             "description": "Only after the coach approved this specific change."},
            "payload_file": {"type": "string", "description": "Absolute path on this server's machine."},
        }, "required": ["plan_id"]},
    ))

    async def _h_get(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_plan_get_strength_workouts(
            plan_id=args["plan_id"], weeks=args.get("weeks"), full=bool(args.get("full", False)),
            save_to=args.get("save_to"))

    async def _h_add(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_plan_add_strength_workouts(
            plan_id=args["plan_id"], workouts=args.get("workouts") or None,
            allow_public=bool(args.get("allow_public", False)),
            skip_if_exists=bool(args.get("skip_if_exists", True)),
            force=bool(args.get("force", False)), dry_run=bool(args.get("dry_run", False)),
            payload_file=args.get("payload_file"))

    handlers["lo_plan_get_strength_workouts"] = _h_get
    handlers["lo_plan_add_strength_workouts"] = _h_add
