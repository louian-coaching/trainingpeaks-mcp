"""Coach-edit sync tools (2026/09/23 教練裁示「四點都做」)：

  lo_diff_week             — what changed in TP since the last week read-back
  lo_render_body           — rebuild 課表本體 lines from structured_workout
  lo_delete_workouts_batch — delete several workouts, each verified gone

Background: the working pattern is "assistant builds → coach edits in the TP
builder → assistant syncs the text". On 2026/09/23 that sync ran seven times
by hand (Yema ×2, 赵祎明 ×1, abu ×4): read back, diff against the build file,
convert % to watts / pace, rewrite, validate. Changes were missed (abu 10/1 and
10/4 surfaced only while syncing 10/3) and one text/graph conflict (赵祎明 10/1,
45 vs 64 min) had to be escalated.

Snapshot: every planned-week read-back through ``lo_get_week_for_validate``
(which the create/update batch tools also use for whole-week read-back) stores
the rows per athlete under ``~/trainingpeaks-mcp/_scratch/snapshots/``. The
diff compares the live week against that snapshot; workouts are matched by id,
so a moved workout is reported as moved, not as delete + add.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from tp_mcp.client.context import athlete_override
from tp_mcp.tools.lo_render import (
    render_for_workout,
    split_description,
    thresholds_from_settings,
)

# ---------------------------------------------------------------------------
# snapshot store
# ---------------------------------------------------------------------------


def snapshot_dir() -> Path:
    env = os.environ.get("TP_LO_SNAPSHOT_DIR")
    return Path(env).expanduser() if env else Path.home() / "trainingpeaks-mcp" / "_scratch" / "snapshots"


def _athlete_key() -> str:
    raw = athlete_override.get() or "self"
    return re.sub(r"[^\w\-]+", "_", str(raw).strip()) or "self"


def _snap_path(key: str | None = None) -> Path:
    return snapshot_dir() / f"{key or _athlete_key()}.json"


def load_snapshot(key: str | None = None) -> dict[str, Any]:
    p = _snap_path(key)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"workouts": {}}


def _day(v: Any) -> str:
    return str(v or "")[:10]


def save_snapshot(rows: list[dict[str, Any]], start: str, end: str, key: str | None = None) -> str | None:
    """Replace the snapshot's [start, end] window with ``rows``. Never raises."""
    try:
        snap = load_snapshot(key)
        ws: dict[str, Any] = snap.get("workouts") or {}
        ws = {i: w for i, w in ws.items() if not (start <= _day(w.get("date")) <= end)}
        for r in rows:
            if r.get("id") is not None:
                ws[str(r["id"])] = r
        snap = {"workouts": ws, "updated": datetime.now().isoformat(timespec="seconds"),
                "last_window": {"start": start, "end": end}}
        p = _snap_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(p)
    except Exception:  # noqa: BLE001 — a snapshot must never break a read-back
        return None


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

_SCALARS = (
    ("title", None),
    ("sport", None),
    ("tss_planned", 0.5),
    ("duration_planned", 1 / 60),   # hours; 1 minute
    ("distance_planned_km", 0.01),
)


def _close(a: Any, b: Any, tol: float | None) -> bool:
    if tol is None:
        return a == b
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def _steps_sig(sw: Any) -> list[Any]:
    """Exact step signature: (reps, [(len, unit, class, targets…)])."""
    if not isinstance(sw, dict):
        return []
    out = []
    for blk in sw.get("structure") or []:
        if not isinstance(blk, dict):
            continue
        reps = (blk.get("length") or {}).get("value")
        steps = []
        for st in blk.get("steps") or []:
            if not isinstance(st, dict):
                continue
            ln = st.get("length") or {}
            tg = tuple((t.get("minValue"), t.get("maxValue"), t.get("unit"))
                       for t in st.get("targets") or [] if isinstance(t, dict))
            steps.append((ln.get("value"), ln.get("unit"), st.get("intensityClass"), tg))
        out.append((reps, tuple(steps)))
    return out


def _text_diff(a: str, b: str, limit: int = 30) -> list[str]:
    import difflib

    return list(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0))[2:2 + limit]


def diff_rows(before: dict[str, dict[str, Any]], after: list[dict[str, Any]], start: str, end: str) -> dict[str, Any]:
    now = {str(w.get("id")): w for w in after if w.get("id") is not None}
    in_window_before = {i: w for i, w in before.items() if start <= _day(w.get("date")) <= end}
    added, deleted, changed = [], [], []
    unchanged = 0
    for wid, w in now.items():
        old = before.get(wid)
        brief = {"id": wid, "date": _day(w.get("date")), "sport": w.get("sport"), "title": w.get("title")}
        if old is None:
            added.append({**brief, "tss_planned": w.get("tss_planned")})
            continue
        ch: dict[str, Any] = {}
        if _day(old.get("date")) != _day(w.get("date")):
            ch["date"] = {"from": _day(old.get("date")), "to": _day(w.get("date"))}
        for f, tol in _SCALARS:
            if not _close(old.get(f), w.get(f), tol):
                ch[f] = {"from": old.get(f), "to": w.get(f)}
        ob, oe = split_description(old.get("description"))
        nb, ne = split_description(w.get("description"))
        if ob.strip() != nb.strip():
            ch["body"] = _text_diff(ob, nb)
        if oe.strip() != ne.strip():
            ch["explanation"] = _text_diff(oe, ne)
        if _steps_sig(old.get("structured_workout")) != _steps_sig(w.get("structured_workout")):
            ch["structure"] = True
        if ch:
            changed.append({**brief, "changes": ch})
        else:
            unchanged += 1
    for wid, w in in_window_before.items():
        if wid not in now:
            deleted.append({"id": wid, "date": _day(w.get("date")), "sport": w.get("sport"), "title": w.get("title")})
    return {"added": added, "deleted": deleted, "changed": changed, "unchanged": unchanged}


async def _settings_thresholds() -> tuple[dict[str, Any], str | None]:
    from tp_mcp.tools.settings import tp_get_athlete_settings

    s = await tp_get_athlete_settings()
    if isinstance(s, dict) and s.get("isError"):
        return {}, f"settings read failed: {s.get('message')}"
    return thresholds_from_settings(s), None


async def lo_diff_week(start_date: str, end_date: str, save_snapshot_after: bool = True,
                       render: bool = True, save_to: str | None = None) -> dict[str, Any]:
    from tp_mcp.tools.lo_tools import lo_get_week_for_validate

    snap = load_snapshot()
    before = snap.get("workouts") or {}
    had_snapshot = any(start_date <= _day(w.get("date")) <= end_date for w in before.values())
    tmp = save_to or str(snapshot_dir() / f"_diff_{_athlete_key()}_{start_date}_{end_date}.json")
    Path(tmp).expanduser().parent.mkdir(parents=True, exist_ok=True)
    week = await lo_get_week_for_validate(start_date=start_date, end_date=end_date, save_to=tmp,
                                          _snapshot=False)
    if isinstance(week, dict) and week.get("isError"):
        return week
    rows = week.get("rows") or []
    out: dict[str, Any] = {"success": True, "date_range": {"start": start_date, "end": end_date},
                           "baseline": snap.get("updated") if had_snapshot else None}
    if not had_snapshot:
        out["warning"] = ("No snapshot covers this window yet — everything is reported as 'added'. "
                          "Snapshots are written by every planned-week read-back from now on.")
    out.update(diff_rows(before, rows, start_date, end_date))
    if render:
        th, err = await _settings_thresholds()
        if err:
            out["render_warning"] = err
        stale = []
        for w in rows:
            r = render_for_workout(w, th)
            if r.get("renderable") and not r.get("in_sync"):
                stale.append({"id": str(w.get("id")), "date": _day(w.get("date")), "sport": w.get("sport"),
                              "title": w.get("title"), "changed_lines": r["changed_lines"],
                              "suggested_body": r["suggested_body"]})
        out["thresholds"] = th
        out["stale_bodies"] = stale
    if save_snapshot_after:
        out["snapshot"] = save_snapshot(rows, start_date, end_date)
    out["readback_json_path"] = str(Path(tmp).expanduser())
    out["week_load"] = week.get("week_load")
    out["next"] = ("stale_bodies[].suggested_body is the graph rendered as text (your own labels kept "
                   "where the numbers match). Explanations are never rewritten — update them yourself, "
                   "then validate (R80 checks explanation numbers against the body).")
    return out


# ---------------------------------------------------------------------------
# render one workout
# ---------------------------------------------------------------------------


async def lo_render_body(workout_id: str, apply: bool = False) -> dict[str, Any]:
    from tp_mcp.tools.lo_tools import _update_verified
    from tp_mcp.tools.workouts import tp_get_workout
    from tp_mcp.tools.workouts_batch import _flatten_readback

    detail = await tp_get_workout(workout_id=str(workout_id))
    if isinstance(detail, dict) and detail.get("isError"):
        return detail
    flat = _flatten_readback(detail)
    th, err = await _settings_thresholds()
    r = render_for_workout(flat, th)
    out: dict[str, Any] = {"workout_id": str(workout_id), "date": _day(flat.get("date")),
                           "sport": flat.get("sport"), "title": flat.get("title"), "thresholds": th}
    if err:
        out["warning"] = err
    if not r.get("renderable"):
        return {**out, "isError": True, "error_code": "NOT_RENDERABLE", "message": r.get("reason")}
    out.update({k: r[k] for k in ("in_sync", "rendered_body", "suggested_body", "changed_lines")})
    if apply and not r["in_sync"]:
        body = r["suggested_body"].rstrip("\n") + "\n"
        new_desc = body + (r["explanation"] if r["explanation"] else "")
        res = await _update_verified(str(workout_id), description=new_desc)
        res.pop("_after", None)
        out["applied"] = bool(res.get("success"))
        out["update"] = res
        if r["explanation"]:
            out["reminder"] = "Body updated from the graph; the explanation below －－ was NOT touched."
        if not res.get("success"):
            out["isError"] = True
            out["error_code"] = "WRITE_NOT_LANDED"
    elif apply:
        out["applied"] = False
        out["note"] = "Already in sync — nothing written."
    out["success"] = not out.get("isError")
    return out


# ---------------------------------------------------------------------------
# batch delete
# ---------------------------------------------------------------------------


async def lo_delete_workouts_batch(workout_ids: list[Any], expect_athlete_name: str | None = None,
                                   dry_run: bool = False, allow_completed: bool = False) -> dict[str, Any]:
    from tp_mcp.tools.profile import tp_get_profile
    from tp_mcp.tools.workouts import tp_delete_workout, tp_get_workout

    ids = [str(i) for i in (workout_ids or []) if str(i).strip()]
    if not ids:
        return {"isError": True, "error_code": "INVALID_ARGS", "message": "workout_ids must be a non-empty list"}
    if expect_athlete_name:
        prof = await tp_get_profile()
        got = str((prof or {}).get("name") or "").strip().lower()
        want = expect_athlete_name.strip().lower()
        if not got or (want not in got and got not in want):
            got_name = (prof or {}).get("name")
            return {"isError": True, "error_code": "ATHLETE_MISMATCH",
                    "message": (f"身分不符：expect_athlete_name='{expect_athlete_name}'，"
                                f"實際 '{got_name}'。一堂都沒刪。")}
    rows: list[dict[str, Any]] = []
    for wid in ids:
        d = await tp_get_workout(workout_id=wid)
        if isinstance(d, dict) and d.get("isError"):
            rows.append({"id": wid, "status": "not_found", "message": d.get("message")})
            continue
        brief = {"id": wid, "date": _day(d.get("date")), "sport": d.get("sport"), "title": d.get("title")}
        completed = bool(d.get("completed")) or bool((d.get("metrics") or {}).get("duration_actual"))
        if completed and not allow_completed:
            rows.append({**brief, "status": "skipped_completed",
                         "message": "has actual data; pass allow_completed=true to delete it"})
            continue
        if dry_run:
            rows.append({**brief, "status": "would_delete"})
            continue
        res = await tp_delete_workout(workout_id=wid)
        if isinstance(res, dict) and res.get("isError"):
            rows.append({**brief, "status": "delete_failed", "message": res.get("message")})
            continue
        chk = await tp_get_workout(workout_id=wid)
        gone = isinstance(chk, dict) and bool(chk.get("isError"))
        rows.append({**brief, "status": "deleted" if gone else "still_present"})
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    bad = {"not_found", "delete_failed", "still_present"}
    out: dict[str, Any] = {"success": not any(r["status"] in bad for r in rows), "dry_run": dry_run,
                           "summary": summary, "results": rows}
    if not out["success"]:
        out["isError"] = True
        out["error_code"] = "DELETE_NOT_VERIFIED"
        out["message"] = "Some workouts were not deleted (see results[].status)."
    return out


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register_lo_sync(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(Tool(
        name="lo_diff_week",
        description=(
            "What changed in a week since the last read-back: added / deleted / moved / changed "
            "workouts (title, sport, TSS, duration, distance, body text, explanation text, "
            "structure), matched by workout id. Also renders every Bike/Run structure as text and "
            "lists `stale_bodies` whose 課表本體 no longer matches the graph, with a suggested body "
            "(coach's own labels kept where numbers match). Use this first when the coach says "
            "'我改計劃了'. Baseline = snapshot written by every planned-week read-back "
            "(lo_get_week_for_validate / batch tools with readback_week_*); the snapshot is "
            "refreshed after the diff unless save_snapshot=false."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "save_snapshot": {"type": "boolean", "default": True},
                "render": {"type": "boolean", "default": True,
                           "description": "Render structures and report stale bodies."},
                "save_to": {"type": "string",
                            "description": "Optional path (on the Mac) for the validate-ready read-back."},
            },
            "required": ["start_date", "end_date"],
        },
    ))
    tools.append(Tool(
        name="lo_render_body",
        description=(
            "Rebuild the 課表本體 lines (above －－) of one Bike/Run workout from its "
            "structured_workout, using the athlete's bike FTP / run threshold from settings "
            "(sport-specific zone groups first). Returns in_sync, rendered_body, suggested_body "
            "(keeps existing lines whose numbers match) and changed_lines. apply=true writes the "
            "suggested body with read-back verification; the explanation below －－ is never touched."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workout_id": {"type": "string"},
                "apply": {"type": "boolean", "default": False},
            },
            "required": ["workout_id"],
        },
    ))
    tools.append(Tool(
        name="lo_delete_workouts_batch",
        description=(
            "Delete several workouts in one call. Each one is read first (date/sport/title are "
            "reported), deleted, then read again to confirm it is gone. Workouts with actual data "
            "are skipped unless allow_completed=true. dry_run lists what would be deleted. "
            "Deleting is irreversible — only after the coach approved it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workout_ids": {"type": "array", "items": {"type": "string"}},
                "expect_athlete_name": {"type": "string",
                                        "description": "Safety check; mismatch = nothing deleted."},
                "dry_run": {"type": "boolean", "default": False},
                "allow_completed": {"type": "boolean", "default": False},
            },
            "required": ["workout_ids"],
        },
    ))

    async def _h_diff(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_diff_week(start_date=args["start_date"], end_date=args["end_date"],
                                  save_snapshot_after=bool(args.get("save_snapshot", True)),
                                  render=bool(args.get("render", True)), save_to=args.get("save_to"))

    async def _h_render(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_render_body(workout_id=args["workout_id"], apply=bool(args.get("apply", False)))

    async def _h_delete(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_delete_workouts_batch(workout_ids=args.get("workout_ids") or [],
                                              expect_athlete_name=args.get("expect_athlete_name"),
                                              dry_run=bool(args.get("dry_run", False)),
                                              allow_completed=bool(args.get("allow_completed", False)))

    handlers["lo_diff_week"] = _h_diff
    handlers["lo_render_body"] = _h_render
    handlers["lo_delete_workouts_batch"] = _h_delete
