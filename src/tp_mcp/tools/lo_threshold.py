"""羅教練 fork-only: ``lo_update_threshold_verified`` — threshold writes with proof.

**Context.** TECH-22 (08/15: ``tp_update_ftp``'s return value ≠ what landed) and
TECH-24 (08/17: ``tp_update_hr_zones`` silently changed the Default set instead
of a sport set) are the last two write paths the fork never sealed; the
threshold SOP survives on "remember to read back". PROC-84 ③ (threshold
freshness, 2026/10/01) means more tests and therefore more threshold changes.
And a threshold change has a second half nobody automates: the already-built
future sessions keep the OLD absolute numbers in their text ("185~214W",
"不准快於 5:10/km", "心率 ≤158") while their %-based structure silently scales.

This tool does both halves:

1. snapshot every zone array → write through the upstream tool → read the
   settings back → the target set's threshold must be the value sent, and
   **no other zone set may have changed** (``COLLATERAL_CHANGE``, TECH-24);
2. scan the next ``scan_days`` of planned sessions for absolute numbers tied to
   that threshold and list each with a proportional suggestion. It never edits
   a session — the fix goes through ``lo_update_workout_verified`` /
   ``lo_render_body`` (PROC-80), with the coach's numbers when they differ.

``dry_run=true`` reads and scans with the proposed value and writes nothing.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# kind -> (settings array, zone workout type, sports whose text to scan)
_KINDS: dict[str, dict[str, Any]] = {
    "ftp_bike": {"key": "powerZones", "wt": "bike", "unit": "W", "sports": ("Bike", "MtnBike", "Brick")},
    "ftp_run": {"key": "powerZones", "wt": "run", "unit": "W", "sports": ("Run",)},
    "run_pace": {"key": "speedZones", "wt": "run", "unit": "/km", "sports": ("Run",)},
    "swim_pace": {"key": "speedZones", "wt": "swim", "unit": "/100m", "sports": ("Swim",)},
    "lthr": {"key": "heartRateZones", "wt": None, "unit": "bpm", "sports": None},
}
_LTHR_SPORTS = {"general": None, "default": None, "bike": ("Bike", "MtnBike", "Brick"),
                "run": ("Run",), "swim": ("Swim",)}
_ZONE_KEYS = ("powerZones", "heartRateZones", "speedZones")

_RANGE = r"\s*[~～\-–—]\s*"
_W_RE = re.compile(r"(?<![\d.])(\d{2,3})(?:" + _RANGE + r"(\d{2,3}))?\s*W\b")
_KM_RE = re.compile(r"(?<![\d:])(\d{1,2}:\d{2})(?:" + _RANGE + r"(\d{1,2}:\d{2}))?"
                    r"\s*(?:/\s*(?:km|公里|k)\b|分/公里|/K\b)")
_100_RE = re.compile(r"(?<![\d:])(\d{1,2}:\d{2})(?:" + _RANGE + r"(\d{1,2}:\d{2}))?\s*/\s*100\s*m")
_HR_RE = re.compile(
    r"(?:心率|HR|hr)[^\d\n]{0,10}(\d{2,3})(?:" + _RANGE + r"(\d{2,3}))?"
    r"|(?<!\d)(\d{2,3})(?:" + _RANGE + r"(\d{2,3}))?\s*(?:bpm|下/分)"
)


def _wtid(kind: str, hr_workout_type: str) -> int:
    from tp_mcp.tools.settings import _ZONE_WTID

    wt = _KINDS[kind]["wt"] if kind != "lthr" else hr_workout_type
    return _ZONE_WTID.get((wt or "general").lower(), 0)


def _find_group(groups: Any, wtid: int) -> tuple[int | None, dict[str, Any] | None]:
    if not isinstance(groups, list):
        return None, None
    for i, g in enumerate(groups):
        if isinstance(g, dict) and g.get("workoutTypeId") == wtid:
            return i, g
    return None, None


def _bands(group: dict[str, Any] | None) -> list[list[Any]]:
    return [[z.get("minimum"), z.get("maximum")] for z in (group or {}).get("zones") or [] if isinstance(z, dict)]


def _pace_seconds(speed_ms: float, metres: float) -> float:
    return metres / speed_ms


def _fmt_pace(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _to_sec(mmss: str) -> int:
    m, s = mmss.split(":")
    return int(m) * 60 + int(s)


def _display(kind: str, threshold: Any) -> str | None:
    if not isinstance(threshold, (int, float)) or threshold <= 0:
        return None
    if kind == "run_pace":
        return _fmt_pace(_pace_seconds(threshold, 1000)) + "/km"
    if kind == "swim_pace":
        return _fmt_pace(_pace_seconds(threshold, 100)) + "/100m"
    return f"{threshold:g}{'W' if kind.startswith('ftp') else ' bpm'}"


def _expected(kind: str, value: Any) -> float:
    from tp_mcp.tools.settings import _parse_pace_to_ms

    if kind == "run_pace":
        return _parse_pace_to_ms(str(value), is_swim=False)
    if kind == "swim_pace":
        return _parse_pace_to_ms(str(value), is_swim=True)
    return float(value)


def _landed_ok(kind: str, expected: float, landed: Any) -> bool:
    if not isinstance(landed, (int, float)):
        return False
    if kind in ("run_pace", "swim_pace"):
        return abs(landed - expected) <= 0.005 * expected
    return round(landed) == round(expected)


def _collateral(before: dict[str, Any], after: dict[str, Any], key: str, idx: int | None) -> list[dict[str, Any]]:
    """Zone sets that changed but were not the target (TECH-24)."""
    out = []
    for k in _ZONE_KEYS:
        b, a = before.get(k) or [], after.get(k) or []
        n = max(len(b), len(a))
        for i in range(n):
            if k == key and i == idx:
                continue
            gb = b[i] if i < len(b) else None
            ga = a[i] if i < len(a) else None
            if gb != ga:
                wt = (ga or gb or {}).get("workoutTypeId") if isinstance(ga or gb, dict) else None
                out.append({"array": k, "index": i, "workoutTypeId": wt,
                            "threshold_before": (gb or {}).get("threshold") if isinstance(gb, dict) else None,
                            "threshold_after": (ga or {}).get("threshold") if isinstance(ga, dict) else None})
    return out


def scan_stale_text(kind: str, old: float, new: float, workouts: list[dict[str, Any]],
                    sports: tuple[str, ...] | None) -> list[dict[str, Any]]:
    """Absolute numbers tied to the old threshold in planned sessions' text, each with
    a proportional suggestion. ``old``/``new`` are the stored thresholds (W, bpm, m/s)."""
    if not old or not new or old == new:
        return []
    rows = []
    for w in workouts:
        if w.get("type") == "completed":
            continue
        if sports is not None and (w.get("sport") or "") not in sports:
            continue
        text = w.get("description") or ""
        if not text:
            continue
        matches = []
        if kind.startswith("ftp"):
            for m in _W_RE.finditer(text):
                nums = [int(x) for x in m.groups() if x]
                sug = [round(n * new / old) for n in nums]
                matches.append((m, "~".join(map(str, sug)) + "W"))
        elif kind in ("run_pace", "swim_pace"):
            rx, unit = (_KM_RE, "/km") if kind == "run_pace" else (_100_RE, "/100m")
            # pace scales with 1/speed
            for m in rx.finditer(text):
                paces = [x for x in m.groups() if x]
                sug = [_fmt_pace(_to_sec(p) * old / new) for p in paces]
                matches.append((m, "~".join(sug) + unit))
        else:
            for m in _HR_RE.finditer(text):
                nums = [int(x) for x in m.groups() if x]
                if not nums or any(n < 80 or n > 220 for n in nums):
                    continue
                matches.append((m, "~".join(str(round(n * new / old)) for n in nums)))
        if not matches:
            continue
        found = []
        for m, sug in matches[:12]:
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start: line_end if line_end != -1 else len(text)].strip()
            in_body = "－－" in text and m.start() < text.find("－－")
            found.append({"old": m.group(0).strip(), "suggest": sug,
                          "where": "body" if in_body else "explanation", "line": line[:80]})
        rows.append({"workout_id": str(w.get("id")), "date": str(w.get("date"))[:10],
                     "sport": w.get("sport"), "title": w.get("title"), "matches": found})
    return rows


def _scaled_groups(kind: str, old: float, new: float, m: re.Match[str]) -> list[tuple[int, str]] | None:
    """(group index, replacement) for every numeric group of one match, or None to skip."""
    out = []
    for gi in range(1, (m.lastindex or 0) + 1):
        v = m.group(gi)
        if not v:
            continue
        if kind.startswith("ftp"):
            out.append((gi, str(round(int(v) * new / old))))
        elif kind in ("run_pace", "swim_pace"):
            out.append((gi, _fmt_pace(_to_sec(v) * old / new)))
        else:
            n = int(v)
            if n < 80 or n > 220:
                return None
            out.append((gi, str(round(n * new / old))))
    return out or None


def rewrite_text(kind: str, old: float, new: float, text: str) -> tuple[str, int]:
    """FORK 2026-11-06: apply the proportional suggestion in place — only the numeric
    groups are replaced, so units, range separators (~ ～ -) and wording stay as written."""
    if not old or not new or old == new or not text:
        return text, 0
    rx = {"ftp_bike": _W_RE, "ftp_run": _W_RE, "run_pace": _KM_RE, "swim_pace": _100_RE}.get(kind, _HR_RE)
    spans: list[tuple[int, int, str]] = []
    hits = 0
    for m in rx.finditer(text):
        groups = _scaled_groups(kind, old, new, m)
        if groups:
            hits += 1
            spans.extend((m.start(g), m.end(g), rep) for g, rep in groups)
    for a, b, rep in sorted(spans, reverse=True):
        text = text[:a] + rep + text[b:]
    return text, hits


async def _settings() -> dict[str, Any]:
    from tp_mcp.tools.settings import tp_get_athlete_settings

    res = await tp_get_athlete_settings()
    if not isinstance(res, dict) or res.get("isError"):
        raise RuntimeError(f"settings read failed: {(res or {}).get('message')}")
    return res.get("settings") or {}


async def _apply_text(kind: str, old: float, new: float, stale: list[dict[str, Any]],
                      by_id: dict[str, dict[str, Any]], mode: str) -> dict[str, Any]:
    """FORK 2026-11-06 (coach menu, 11/06 CP 270→278 = 15 sessions re-typed by hand).

    preview: new description per session, nothing written.
    write: lo_update_workout_verified(description=…) per session, then the body is
    checked against the graph (lo_render_body) and re-rendered when they disagree —
    the structure is %-based and already follows the new threshold, so the graph wins.
    """
    from tp_mcp.tools.lo_sync import lo_render_body
    from tp_mcp.tools.lo_tools import lo_update_workout_verified

    rows, written, failed = [], 0, 0
    for s in stale:
        wid = s["workout_id"]
        text = (by_id.get(wid) or {}).get("description") or ""
        new_text, n = rewrite_text(kind, old, new, text)
        row: dict[str, Any] = {"workout_id": wid, "date": s["date"], "title": s["title"], "replaced": n}
        if mode == "preview" or new_text == text:
            row["new_description"] = new_text
            rows.append(row)
            continue
        res = await lo_update_workout_verified(wid, description=new_text)
        row["written"] = bool(res.get("success"))
        if not row["written"]:
            failed += 1
            row["error"] = res.get("error_code") or res.get("message")
            rows.append(row)
            continue
        written += 1
        try:
            r = await lo_render_body(wid, apply=True)
            row["body_vs_graph"] = ("re-rendered" if r.get("applied") else
                                    "in_sync" if r.get("in_sync") else
                                    r.get("error_code") or "not_checked")
        except Exception as e:  # noqa: BLE001 — the text write already landed
            row["body_vs_graph"] = f"check_failed: {e}"
        rows.append(row)
    return {"mode": mode, "sessions": rows, "written": written, "failed": failed,
            "note": ("比例換算、四捨五入；教練處方取整（如 4:51→4:50）或 CP／FTP 數值本身的敘述句"
                     "（「CP 270」）不會自動改，寫入後仍要人工看一遍並跑 validate R80。")}


async def lo_update_threshold_verified(
    kind: str,
    value: Any,
    hr_workout_type: str = "general",
    max_hr: int | None = None,
    resting_hr: int | None = None,
    scan_days: int = 28,
    dry_run: bool = False,
    today: str | None = None,
    apply_text: str = "none",
) -> dict[str, Any]:
    from tp_mcp.tools.lo_review import athlete_identity
    from tp_mcp.tools.settings import tp_update_ftp, tp_update_hr_zones, tp_update_speed_zones
    from tp_mcp.tools.workouts import tp_get_workouts

    if apply_text not in ("none", "preview", "write"):
        return {"isError": True, "error_code": "INVALID_ARGS",
                "message": "apply_text must be none|preview|write"}
    if kind not in _KINDS:
        return {"isError": True, "error_code": "INVALID_ARGS",
                "message": f"kind must be one of {sorted(_KINDS)}"}
    if kind == "lthr" and hr_workout_type.lower() not in _LTHR_SPORTS:
        return {"isError": True, "error_code": "INVALID_ARGS",
                "message": f"hr_workout_type must be one of {sorted(_LTHR_SPORTS)}"}
    try:
        expected = _expected(kind, value)
    except (ValueError, TypeError) as e:
        return {"isError": True, "error_code": "INVALID_ARGS", "message": str(e)}

    ident = await athlete_identity()
    spec = _KINDS[kind]
    key = spec["key"]
    wtid = _wtid(kind, hr_workout_type)
    try:
        before = copy.deepcopy(await _settings())
    except RuntimeError as e:
        return {"isError": True, "error_code": "API_ERROR", "message": str(e)}
    idx, group = _find_group(before.get(key), wtid)
    old = (group or {}).get("threshold")
    out: dict[str, Any] = {
        **ident, "kind": kind, "dry_run": dry_run,
        "zone_set": {"array": key, "workoutTypeId": wtid, "exists": group is not None},
        "before": {"threshold": old, "display": _display(kind, old)},
        "sent": {"value": value, "threshold": expected, "display": _display(kind, expected)},
    }
    if group is None:
        out["warnings"] = [f"athlete has no {key} set for workoutTypeId={wtid}; the upstream tool would "
                           "fall back to the Default set (TECH-24) — create the sport set in TP first."]
        if not dry_run:
            out.update(isError=True, error_code="NO_SPORT_ZONE_SET",
                       message=out["warnings"][0] + " Nothing written.")
            return out

    if not dry_run:
        if kind == "ftp_bike":
            res = await tp_update_ftp(ftp=int(round(expected)), workout_type="bike")
        elif kind == "ftp_run":
            res = await tp_update_ftp(ftp=int(round(expected)), workout_type="run")
        elif kind == "run_pace":
            res = await tp_update_speed_zones(run_threshold_pace=str(value))
        elif kind == "swim_pace":
            res = await tp_update_speed_zones(swim_threshold_pace=str(value))
        else:
            res = await tp_update_hr_zones(threshold_hr=int(round(expected)), max_hr=max_hr,
                                           resting_hr=resting_hr, workout_type=hr_workout_type)
        out["upstream"] = {k: v for k, v in (res or {}).items() if k not in ("zones", "updated")}
        try:
            after = await _settings()
        except RuntimeError as e:
            out.update(isError=True, error_code="READBACK_FAILED", message=str(e))
            return out
        _, g_after = _find_group(after.get(key), wtid)
        landed = (g_after or {}).get("threshold")
        out["after"] = {"threshold": landed, "display": _display(kind, landed)}
        out["zones"] = {"before": _bands(group), "after": _bands(g_after)}
        collateral = _collateral(before, after, key, idx)
        ok = _landed_ok(kind, expected, landed)
        out["landed"] = ok
        if collateral:
            out["collateral"] = collateral
        if res and res.get("isError"):
            out.update(isError=True, error_code=res.get("error_code") or "API_ERROR",
                       message=f"upstream write failed: {res.get('message')}")
        elif collateral:
            out.update(isError=True, error_code="COLLATERAL_CHANGE",
                       message=f"{len(collateral)} other zone set(s) changed (TECH-24) — check in TP.")
        elif not ok:
            out.update(isError=True, error_code="WRITE_NOT_LANDED",
                       message=f"sent {out['sent']['display']}, settings read back {out['after']['display']}.")
        else:
            out["success"] = True
        if kind == "lthr" and (max_hr or resting_hr) and g_after:
            out["anchors"] = {"maximumHeartRate": g_after.get("maximumHeartRate"),
                              "restingHeartRate": g_after.get("restingHeartRate")}

    # Second half: sessions already built with the old numbers.
    start = today or dt.date.today().isoformat()
    end = (dt.date.fromisoformat(start) + dt.timedelta(days=max(0, int(scan_days)))).isoformat()
    sports = spec["sports"] if kind != "lthr" else _LTHR_SPORTS[hr_workout_type.lower()]
    new_thr = out.get("after", {}).get("threshold") if not dry_run else expected
    try:
        listed = await tp_get_workouts(start_date=start, end_date=end, workout_filter="planned")
        workouts = (listed or {}).get("workouts") or [] if not (listed or {}).get("isError") else []
        stale = scan_stale_text(kind, float(old or 0), float(new_thr or 0), workouts, sports)
        out["stale_text"] = {"window": [start, end], "scanned": len(workouts), "workouts": stale}
        if stale and apply_text != "none":
            by_id = {str(w.get("id")): w for w in workouts}
            mode = "preview" if dry_run or out.get("isError") else apply_text
            out["stale_text"]["apply"] = await _apply_text(kind, float(old), float(new_thr), stale, by_id, mode)
        if stale:
            out["next_steps"] = (
                f"{len(stale)} planned session(s) still quote the old {spec['unit']} numbers. Structures are "
                "%-based and already follow the new threshold; fix the TEXT: body lines via lo_render_body "
                "(apply), explanation numbers via lo_update_workout_verified. Suggestions are proportional "
                "— round to the coach's prescription where it differs.")
    except Exception as e:  # noqa: BLE001 — the scan is advisory
        out["stale_text"] = {"error": str(e)}
    return out


def register_lo_threshold(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(Tool(
        name="lo_update_threshold_verified",
        description=(
            "Change ONE threshold with proof (seals TECH-22/24): kind=ftp_bike|ftp_run (W), "
            "run_pace ('4:36/km'), swim_pace ('1:38/100m'), lthr (bpm; hr_workout_type "
            "general|bike|run|swim). Snapshots all zone sets, writes via the upstream tool, reads "
            "settings back: success only if the target set holds the sent value AND no other zone "
            "set changed (COLLATERAL_CHANGE). Then scans the next scan_days of planned sessions for "
            "absolute numbers tied to the old threshold (W / pace / heart rate) and lists each with "
            "a proportional suggestion; apply_text=preview|write rewrites those numbers in place "
            "(write = verified, then bodies re-rendered from the graph). dry_run=true: read + scan "
            "with the proposed value, write nothing. Ask the coach before writing (權限邊界)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": sorted(_KINDS)},
                "value": {"type": ["number", "string"],
                          "description": "W or bpm as a number; pace as 'M:SS/km' or 'M:SS/100m'."},
                "hr_workout_type": {"type": "string", "enum": sorted(_LTHR_SPORTS), "default": "general"},
                "max_hr": {"type": "integer", "description": "lthr only: also store max HR anchor."},
                "resting_hr": {"type": "integer", "description": "lthr only: also store resting HR anchor."},
                "scan_days": {"type": "integer", "default": 28},
                "dry_run": {"type": "boolean", "default": False},
                "today": {"type": "string", "description": "YYYY-MM-DD scan start; default today."},
                "apply_text": {"type": "string", "enum": ["none", "preview", "write"], "default": "none",
                               "description": "none: list only. preview: return each session's new "
                                              "description (proportional numbers swapped in place). "
                                              "write: write them verified, then re-render bodies that "
                                              "disagree with the graph. dry_run or a failed threshold "
                                              "write downgrades write→preview."},
            },
            "required": ["kind", "value"],
        },
    ))

    async def _h(args: dict[str, Any]) -> dict[str, Any]:
        return await lo_update_threshold_verified(
            kind=args["kind"], value=args["value"],
            hr_workout_type=str(args.get("hr_workout_type", "general")),
            max_hr=args.get("max_hr"), resting_hr=args.get("resting_hr"),
            scan_days=int(args.get("scan_days", 28)), dry_run=bool(args.get("dry_run", False)),
            today=args.get("today"), apply_text=str(args.get("apply_text", "none")),
        )

    handlers["lo_update_threshold_verified"] = _h
