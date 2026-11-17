"""羅教練 fork-only: ``lo_load_baseline`` — what the coach actually wants this athlete's week to look like.

**Context (2026/11/17 建課段盤點).** ``learning/coach_edits.jsonl`` showed the coach still
editing 100+ sessions a week: the load I planned was raised by a median +13~18%, easy
rides / easy runs / rest days were swapped for aerobic sessions, and the most-edited
sessions were the aerobic ones, not the quality ones. All three are the same mistake —
planning from my own previous draft instead of from what the coach left on the calendar.

One read-only call before building a week answers, per athlete:

* **load** — for each of the last ``weeks`` full weeks: the tri TSS as it stands now
  (= after the coach's edits) and what I had planned (= now minus the logged edits);
* **mix** — how many easy sessions / rest days / aerobic sessions those weeks hold
  after the coach's edits, and how many the coach removed or added;
* **aerobic templates** — last week's aerobic sessions as the coach left them;
* **precheck** — when the target week is already built: its load and easy+rest count
  against those norms.

It reports numbers and hints only; the week type and the load stay the coach's call.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import statistics
from typing import Any

from tp_mcp.client.context import athlete_override

logger = logging.getLogger(__name__)

_TRI = {"Swim", "Bike", "Run"}
_EASY_WORDS = ("輕鬆", "恢復")
_AEROBIC_WORDS = ("有氧", "長距離", "長跑")


def _shift(day: str, days: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=days)).isoformat()


def _has(title: Any, words: tuple[str, ...]) -> bool:
    return any(w in str(title or "") for w in words)


def _mix(rows: list[dict[str, Any]]) -> dict[str, int]:
    easy = rest = aerobic = 0
    for r in rows:
        sport, title = r.get("sport"), r.get("title")
        if sport in ("DayOff", "Day Off"):
            rest += 1
        elif sport in _TRI and (r.get("planned") or r.get("tss_planned")):
            if _has(title, _EASY_WORDS):
                easy += 1
            elif _has(title, _AEROBIC_WORDS):
                aerobic += 1
    return {"easy": easy, "rest": rest, "aerobic": aerobic}


def read_edits(athlete_keys: set[str]) -> list[dict[str, Any]]:
    """Coach edits logged for this athlete (by any of the keys the athlete was called with)."""
    from tp_mcp.tools.lo_sync import learning_dir

    d = learning_dir()
    path = d / "coach_edits.jsonl" if d else None
    if path is None or not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get("athlete")) in athlete_keys and row.get("kind"):
            out.append(row)
    return out


def week_delta(edits: list[dict[str, Any]], start: str, end: str) -> dict[str, Any]:
    """What the coach's edits did to one week: tri TSS added on top of my plan, and the
    easy / rest sessions removed and aerobic sessions added."""
    tss = 0.0
    removed_easy_rest = added_aerobic = n = 0
    for e in edits:
        day = str(e.get("date") or "")[:10]
        if not (start <= day <= end):
            continue
        n += 1
        sport, title, kind = e.get("sport"), e.get("title"), e.get("kind")
        easy_or_rest = sport in ("DayOff", "Day Off") or (sport in _TRI and _has(title, _EASY_WORDS))
        if kind == "deleted" and easy_or_rest:
            removed_easy_rest += 1
        if kind == "added" and sport in _TRI and _has(title, _AEROBIC_WORDS):
            added_aerobic += 1
        if sport not in _TRI:
            continue
        if kind == "changed":
            t = (e.get("changes") or {}).get("tss_planned") or {}
            a, b = t.get("from"), t.get("to")
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                tss += b - a
        elif kind in ("added", "deleted") and isinstance(e.get("tss_planned"), (int, float)):
            tss += e["tss_planned"] if kind == "added" else -e["tss_planned"]
    return {"tss": round(tss, 1), "edits": n, "removed_easy_rest": removed_easy_rest,
            "added_aerobic": added_aerobic}


def _median(vals: list[float]) -> float | None:
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(statistics.median(vals), 2) if vals else None


async def lo_load_baseline(week_start: str, weeks: int = 4, today: str | None = None) -> dict[str, Any]:
    from tp_mcp.tools.lo_review import athlete_identity, summarize_workouts
    from tp_mcp.tools.workouts import tp_get_workouts

    try:
        dt.date.fromisoformat(week_start)
    except ValueError as e:
        return {"isError": True, "error_code": "INVALID_ARGS", "message": str(e)}
    weeks = max(1, min(int(weeks), 8))
    today = today or dt.date.today().isoformat()
    ident = await athlete_identity()
    keys = {str(athlete_override.get() or ""), str(ident.get("athlete_id") or ""),
            str(ident.get("athlete_name") or "")} - {""}
    edits = read_edits(keys)

    async def week(start: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        end = _shift(start, 6)
        res = await tp_get_workouts(start_date=start, end_date=end, workout_filter="all")
        if res.get("isError"):
            raise RuntimeError(f"{start}: {res.get('error_code')} {res.get('message')}")
        summ = summarize_workouts(res.get("workouts") or [], today=today)
        rows = summ.get("workouts") or []
        coach = float((summ.get("totals") or {}).get("tss_planned_tri") or 0.0)
        d = week_delta(edits, start, end)
        mine = round(coach - d["tss"], 1)
        return ({"week": start, "mine_tri": mine, "coach_tri": round(coach, 1),
                 "ratio": round(coach / mine, 2) if mine > 0 else None, **_mix(rows),
                 "coach_removed_easy_rest": d["removed_easy_rest"], "coach_added_aerobic": d["added_aerobic"],
                 "edits": d["edits"]}, rows)

    try:
        past = [await week(_shift(week_start, -7 * i)) for i in range(weeks, 0, -1)]
        target_row, target_rows = await week(week_start)
    except Exception as e:  # noqa: BLE001
        logger.exception("baseline read failed")
        return {"isError": True, "error_code": "API_ERROR", "message": f"{type(e).__name__}: {e}", **ident}

    rows = [p[0] for p in past]
    last_row, last_rows = past[-1]
    # Weeks far below the latest one (before the athlete started, off-season, race week)
    # are not what a normal week looks like — 11/17 實測: two pre-start weeks halved the
    # aerobic-session norm of an athlete who had only trained with us for two weeks.
    usable = [r for r in rows if r["coach_tri"] >= 0.5 * last_row["coach_tri"]] or rows
    norms = {"ratio_median": _median([r["ratio"] for r in usable if r["edits"]]),
             "easy_plus_rest_median": _median([r["easy"] + r["rest"] for r in usable]),
             "aerobic_median": _median([r["aerobic"] for r in usable]),
             "coach_tri_median": _median([r["coach_tri"] for r in usable]),
             "last_week_coach_tri": last_row["coach_tri"],
             "weeks_used": [r["week"] for r in usable]}
    templates = [{k: r.get(k) for k in ("id", "date", "sport", "title", "planned", "unit", "tss_planned")}
                 for r in last_rows
                 if r.get("sport") in _TRI and _has(r.get("title"), _AEROBIC_WORDS)
                 and (r.get("planned") or r.get("tss_planned"))]
    out: dict[str, Any] = {**ident, "week_start": week_start, "weeks": rows, "norms": norms,
                           "aerobic_templates_last_week": templates}
    built = [r for r in target_rows if r.get("sport") in _TRI and (r.get("planned") or r.get("tss_planned"))]
    if built:
        hints: list[str] = []
        er = target_row["easy"] + target_row["rest"]
        norm = norms["easy_plus_rest_median"]
        if norm is not None and er - norm >= 1:        # PROC-102 (11/17 教練裁示): 多 1 堂就提
            hints.append(f"輕鬆課＋休息日 {er} 堂，近 {weeks} 週教練改後的中位數是 {norm:g}")
        if last_row["coach_tri"] and target_row["coach_tri"] < 0.95 * last_row["coach_tri"]:
            hints.append(f"三項 {target_row['coach_tri']:g} 低於上週教練改後的 {last_row['coach_tri']:g}"
                         "（減量、比賽、賽後週屬正常）")
        out["precheck"] = {"tri": target_row["coach_tri"], "mine_tri": target_row["mine_tri"],
                           "ratio": target_row["ratio"], "edits": target_row["edits"],
                           "easy": target_row["easy"],
                           "rest": target_row["rest"], "aerobic": target_row["aerobic"], "hints": hints}
    out["note"] = ("coach_tri＝日曆現況（教練改後）；mine_tri＝現況扣掉 coach_edits.jsonl 記到的改動。"
                   "沒被 lo_diff_week 掃過的週 edits=0、比值不列入中位數。數字只供送審，量級與週型由教練決定。")
    return out


def register_lo_baseline(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(Tool(
        name="lo_load_baseline",
        description=(
            "排課前置：這位選手近 N 個完整週「我排的三項 TSS → 教練改後」與比值、教練改後每週的"
            "輕鬆課／休息日／有氧課數、教練刪掉的輕鬆課與休息日數、上一週教練改後的有氧課清單（當範本）；"
            "目標週已建課時另回 precheck（量與輕鬆課＋休息日數對照常態的提示）。唯讀，只給數字與提示。"),
        input_schema={"type": "object", "properties": {
            "athlete": {"type": "string", "description": "Target athlete name or ID (coach accounts only)."},
            "week_start": {"type": "string", "description": "要排的那週週一 YYYY-MM-DD"},
            "weeks": {"type": "integer", "default": 4, "description": "往前看幾個完整週（1–8）"},
            "today": {"type": "string", "description": "YYYY-MM-DD; default server local date."}},
            "required": ["week_start"]},
    ))

    async def _h(a: dict[str, Any]) -> dict[str, Any]:
        return await lo_load_baseline(a["week_start"], int(a.get("weeks", 4)), a.get("today"))

    handlers["lo_load_baseline"] = _h
