"""lo_render_body — rebuild the 課表本體 lines from a workout's structured_workout.

Why (2026/09/23 教練裁示「四點都做」第 2 點)：
  The coach edits workouts in the TP builder (the graph = structured_workout);
  the text lines above ``－－`` in ``description`` do not follow. The same day
  seven syncs were done by hand: read back, convert % to watts / pace, rewrite.
  One of them (赵祎明 10/1) had text 45 min vs graph 64 min and had to be
  escalated. This module makes the graph the single source for the body lines.

Scope: Bike / Run (the sports that carry a structure). Swim has no structure.
Only the body (above ``－－``) is rendered; the explanation below is never
touched — it is the coach's voice and needs judgement (see validate R80 for
the mechanical part of keeping it in step).

Formatting follows the house style seen in method/2·4 and real workouts:
  - bike:  ``- 熱身騎10分鐘@133~172W`` / ``- 6分鐘@225~252W+恢復3分鐘@133~159W, 4組``
           ``- 緩騎5分鐘@133~159W``; >60 min → ``1小時``／``1小時15分`` (BIKE-12);
           cadence target → ``（100~120rpm）`` after the watts (BIKE-08)
  - run:   ``- 暖身跑5分鐘+伸展`` / ``- 3公里@5:07~4:31/km`` / ``- 慢跑恢復2分鐘``
           ``- 緩跑5分鐘`` / ``- 伸展``; easy single-step → the 鼻呼吸 template
  - watts: FTP × % rounded half-up (270W×75% = 203W, same as lo_verify)
  - pace:  threshold_sec × 100 / % rounded half-up to the second; slow end first
"""

from __future__ import annotations

import math
import re
from typing import Any

SPLIT = "－－"


# ---------------------------------------------------------------------------
# thresholds
# ---------------------------------------------------------------------------


def _group(groups: Any, wtid: int) -> dict[str, Any] | None:
    if not isinstance(groups, list):
        return None
    for g in groups:
        if isinstance(g, dict) and g.get("workoutTypeId") == wtid:
            return g
    return None


def thresholds_from_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """bike_ftp / run_ftp (W) and run_pace_sec (s/km) from tp_get_athlete_settings.

    Sport-specific groups win (workoutTypeId 2 bike, 3 run); the generic group 0
    is a fallback only for bike FTP — TP's generic power group is often stale
    (abu: 20W, 赵祎明: 240 vs bike 230), which is why the card files say
    "以 bike 專項為準".
    """
    s = settings.get("settings", settings) if isinstance(settings, dict) else {}
    power = s.get("powerZones") or []
    speed = s.get("speedZones") or []
    out: dict[str, Any] = {}
    bike = _group(power, 2) or _group(power, 0)
    if bike and bike.get("threshold"):
        out["bike_ftp"] = float(bike["threshold"])
        out["bike_ftp_source"] = "bike" if bike.get("workoutTypeId") == 2 else "generic"
    run_p = _group(power, 3)
    if run_p and run_p.get("threshold"):
        out["run_ftp"] = float(run_p["threshold"])
    run_s = _group(speed, 3)
    if run_s and run_s.get("threshold"):
        out["run_pace_sec"] = 1000.0 / float(run_s["threshold"])
    return out


# ---------------------------------------------------------------------------
# number formatting
# ---------------------------------------------------------------------------


def half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def fmt_time(sec: float, big_hours: bool = True) -> str:
    sec = int(round(sec))
    if big_hours and sec >= 3600:
        h, rem = divmod(sec, 3600)
        m = rem // 60
        return f"{h}小時" + (f"{m}分" if m else "")
    if sec < 60:
        return f"{sec}秒"
    m, s = divmod(sec, 60)
    return f"{m}分鐘" if s == 0 else f"{m}分{s}秒"


def fmt_dist(m: float) -> str:
    m = float(m)
    if m >= 1000:
        km = m / 1000
        return f"{int(km)}公里" if abs(km - round(km)) < 1e-9 else f"{km:g}公里"
    return f"{int(round(m))}公尺"


def fmt_pace(sec: float) -> str:
    s = half_up(sec)
    return f"{s // 60}:{s % 60:02d}"


def _targets(step: dict[str, Any]) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    main = cad = None
    for t in step.get("targets") or []:
        if not isinstance(t, dict):
            continue
        lo, hi = t.get("minValue"), t.get("maxValue")
        if lo is None and hi is None:
            continue
        lo = float(lo if lo is not None else hi)
        hi = float(hi if hi is not None else lo)
        if t.get("unit") == "roundOrStridePerMinute":
            cad = (lo, hi)
        elif main is None:
            main = (lo, hi)
    return main, cad


def _intensity(pct: tuple[float, float] | None, metric: str, sport: str, th: dict[str, Any]) -> str:
    if not pct:
        return ""
    lo, hi = pct
    if metric == "percentOfThresholdPace":
        thr = th.get("run_pace_sec")
        if not thr or lo <= 0:
            return f"@{lo:g}-{hi:g}%"
        slow, fast = thr * 100 / lo, thr * 100 / hi
        return f"@{fmt_pace(slow)}~{fmt_pace(fast)}/km"
    # percentOfFtp (bike, or power-run)
    ftp = th.get("run_ftp") if sport == "Run" else th.get("bike_ftp")
    if not ftp:
        return f"@{lo:g}-{hi:g}%FTP"
    return f"@{half_up(ftp * lo / 100)}~{half_up(ftp * hi / 100)}W"


def _cad(cad: tuple[float, float] | None) -> str:
    return f"（{cad[0]:g}~{cad[1]:g}rpm）" if cad else ""


def _length(step: dict[str, Any]) -> tuple[str, float]:
    ln = step.get("length") or {}
    unit = str(ln.get("unit") or "second").lower()
    val = float(ln.get("value") or 0)
    if unit in ("meter", "metre", "meters"):
        return "m", val
    if unit in ("kilometer", "kilometre"):
        return "m", val * 1000
    if unit == "minute":
        return "s", val * 60
    return "s", val


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _part(step: dict[str, Any], *, sport: str, metric: str, th: dict[str, Any],
          role: str, big_hours: bool, easy: bool = False) -> str:
    """One step → text. role: warm | cool | rest | work."""
    kind, val = _length(step)
    pct, cad = _targets(step)
    inten = _intensity(pct, metric, sport, th)
    amount = fmt_dist(val) if kind == "m" else fmt_time(val, big_hours)
    if sport == "Run":
        if easy and role == "work" and kind == "s" and pct and pct[1] <= 80:
            return f"輕鬆慢跑{amount}，配速不限，以能夠只以鼻子呼吸為原則。"
        if role == "work" and kind == "s" and val <= 30 and pct and pct[0] >= 95:
            # keep the target: "30秒@3:59~3:49/km" / "30秒@301~316W"; bare "加速跑" only when no target
            return f"{amount}{inten}" if inten and not easy else f"{amount}加速跑"
        if role == "warm" and kind == "s":
            return f"暖身跑{amount}+伸展"
        if role == "cool" and kind == "s":
            return f"緩跑{amount}"
        if role == "rest" and kind == "s":
            return f"慢跑恢復{amount}"
        return f"{amount}{inten}"
    prefix = {"warm": "熱身騎", "cool": "緩騎", "rest": "恢復"}.get(role, "")
    return f"{prefix}{amount}{inten}{_cad(cad)}"


def _role(step: dict[str, Any], *, first: bool, last: bool, in_rep: bool) -> str:
    cls = str(step.get("intensityClass") or "").lower()
    if cls == "warmup" and first and not in_rep:
        return "warm"
    if cls == "cooldown" and last and not in_rep:
        return "cool"
    if cls in ("rest", "recovery"):
        return "rest"
    return "work"


def render_body_lines(sw: dict[str, Any], sport: str, th: dict[str, Any], title: str = "") -> list[str]:
    """structured_workout → list of ``- …`` lines (no ``－－``)."""
    blocks = [b for b in (sw.get("structure") or []) if isinstance(b, dict)]
    metric = str(sw.get("primaryIntensityMetric") or "percentOfFtp")
    lines: list[str] = []
    # easy-run template: one single-step time block, nothing else
    if sport == "Run" and len(blocks) == 1:
        steps = blocks[0].get("steps") or []
        reps = int((blocks[0].get("length") or {}).get("value") or 1)
        if len(steps) == 1 and reps == 1 and _length(steps[0])[0] == "s" and "輕鬆" in (title or ""):
            return ["- 伸展",
                    f"- 輕鬆慢跑{fmt_time(_length(steps[0])[1])}，配速不限，以能夠只以鼻子呼吸為原則。",
                    "- 伸展"]
    easy = sport == "Run" and "輕鬆" in (title or "")
    warm_seen = False
    for bi, blk in enumerate(blocks):
        steps = [s for s in (blk.get("steps") or []) if isinstance(s, dict)]
        reps = int((blk.get("length") or {}).get("value") or 1)
        is_rep = str(blk.get("type") or "").lower() == "repetition" or reps > 1 or len(steps) > 1
        if not steps:
            continue
        if not is_rep:
            st = steps[0]
            role = _role(st, first=(bi == 0 and not warm_seen), last=(bi == len(blocks) - 1), in_rep=False)
            if role == "warm":
                warm_seen = True
            text = _part(st, sport=sport, metric=metric, th=th, role=role, big_hours=True, easy=easy)
            if text.startswith("輕鬆慢跑") and not lines:
                lines.append("- 伸展")
            lines.append("- " + text)
            continue
        parts = [
            _part(st, sport=sport, metric=metric, th=th,
                  role=_role(st, first=False, last=False, in_rep=True), big_hours=False, easy=easy)
            for st in steps
        ]
        short_first = (sport == "Run" and _length(steps[0])[0] == "s" and _length(steps[0])[1] <= 30
                       and _role(steps[0], first=False, last=False, in_rep=True) == "work")
        joiner = ", " if short_first or any(p.endswith("加速跑") for p in parts) else "+"
        line = "- " + joiner.join(parts)
        if reps > 1:
            line += f", {reps}組"
        lines.append(line)
    # house style: a run body ends with 伸展; warm-up line already carries +伸展
    if sport == "Run" and (not lines or lines[-1] != "- 伸展"):
        lines.append("- 伸展")
    return lines


# ---------------------------------------------------------------------------
# comparison (numbers, not labels: 熱身跑 vs 暖身跑 is the coach's wording)
# ---------------------------------------------------------------------------

_TOK_RX = re.compile(
    r"(?P<h>\d+)小時(?:(?P<hm>\d+)分)?"
    r"|(?P<m>\d+)分(?:(?P<ms>\d+)秒|鐘)?"
    r"|(?P<s>\d+)秒"
    r"|(?P<km>\d+(?:\.\d+)?)公里"
    r"|(?P<mt>\d+)公尺"
    r"|(?P<w>\d+~\d+)W"
    r"|(?P<p>\d:\d\d~\d:\d\d)"
    r"|(?P<rpm>\d+~\d+)rpm"
    r"|(?P<reps>\d+)[組趟]"
)


def _tok_eq(a: str, b: str) -> bool:
    """Token equality with rounding tolerance.

    The graph stores integer percentages that were themselves rounded from the
    text, so converting back can move a watt or a pace second (4:57 ↔ 4:59,
    135 ↔ 136W). Times, distances, reps and cadence must match exactly.
    """
    if a == b:
        return True
    if a[0] != b[0] or a[0] not in "wp":
        return False
    try:
        if a[0] == "w":
            x = [int(v) for v in a[1:].split("~")]
            y = [int(v) for v in b[1:].split("~")]
            return all(abs(i - j) <= max(2, round(0.01 * max(i, j))) for i, j in zip(x, y, strict=False))
        def sec(v: str) -> int:
            m, s_ = v.split(":")
            return int(m) * 60 + int(s_)
        x = [sec(v) for v in a[1:].split("~")]
        y = [sec(v) for v in b[1:].split("~")]
        return all(abs(i - j) <= 3 for i, j in zip(x, y, strict=False))
    except (ValueError, IndexError):
        return False


def _drop_short_intensity(sig: tuple[str, ...]) -> tuple[str, ...]:
    """Strides / sprints (≤30 s) are written either with a target
    ("30秒@301~316W") or without ("20秒衝刺跑"): the target is optional text,
    so drop a pace/power token that directly follows a ≤30 s time token."""
    out: list[str] = []
    skip = False
    for i, t in enumerate(sig):
        if skip:
            skip = False
            continue
        out.append(t)
        if t[0] == "t" and t[1:].isdigit() and int(t[1:]) <= 30 and i + 1 < len(sig) and sig[i + 1][0] in "wp":
            skip = True
    return tuple(out)


def sig_eq(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    a, b = _drop_short_intensity(a), _drop_short_intensity(b)
    return len(a) == len(b) and all(_tok_eq(x, y) for x, y in zip(a, b, strict=True))


def line_signature(line: str) -> tuple[str, ...]:
    toks: list[str] = []
    for m in _TOK_RX.finditer(line.replace(" ", "")):
        g = m.groupdict()
        if g["h"]:
            toks.append(f"t{int(g['h']) * 3600 + int(g['hm'] or 0) * 60}")
        elif g["m"]:
            toks.append(f"t{int(g['m']) * 60 + int(g['ms'] or 0)}")
        elif g["s"]:
            toks.append(f"t{int(g['s'])}")
        elif g["km"]:
            toks.append(f"d{int(round(float(g['km']) * 1000))}")
        elif g["mt"]:
            toks.append(f"d{int(g['mt'])}")
        elif g["w"]:
            toks.append(f"w{g['w']}")
        elif g["p"]:
            toks.append(f"p{g['p']}")
        elif g["rpm"]:
            toks.append(f"c{g['rpm']}")
        elif g["reps"]:
            toks.append(f"x{g['reps']}")
    return tuple(toks)


def split_description(desc: str | None) -> tuple[str, str]:
    d = desc or ""
    if SPLIT in d:
        body, rest = d.split(SPLIT, 1)
        return body, SPLIT + rest
    return d, ""


def _sig_lines(body: str) -> list[tuple[str, tuple[str, ...]]]:
    out = []
    for ln in body.splitlines():
        ln = ln.rstrip()
        if not ln.strip():
            continue
        out.append((ln, line_signature(ln)))
    return out


def compare_body(current_body: str, rendered: list[str]) -> dict[str, Any]:
    """Match rendered lines to current lines by numeric signature.

    Returns ``{in_sync, suggested_body, changed_lines}``. A current line whose
    numbers match a rendered line is KEPT verbatim (keeps the coach's labels
    and bracket notes); lines without numbers (``- 伸展``) are kept when the
    rendered body also has them.
    """
    cur = _sig_lines(current_body)
    pool = [(ln, sig) for ln, sig in cur if sig]
    used = [False] * len(pool)
    suggested: list[str] = []
    changed: list[dict[str, str]] = []
    for r in rendered:
        sig = line_signature(r)
        if not sig:
            suggested.append(r)
            continue
        hit = next((i for i, (_, s) in enumerate(pool) if not used[i] and sig_eq(s, sig)), None)
        if hit is not None:
            used[hit] = True
            suggested.append(pool[hit][0])
        else:
            suggested.append(r)
            changed.append({"new": r})
    for i, (ln, _) in enumerate(pool):
        if not used[i]:
            changed.append({"old": ln})
    cur_sigs = [s for _, s in pool]
    ren_sigs = [line_signature(r) for r in rendered if line_signature(r)]
    in_sync = len(cur_sigs) == len(ren_sigs) and all(sig_eq(a, b) for a, b in zip(cur_sigs, ren_sigs, strict=True))
    return {
        "in_sync": in_sync,
        "suggested_body": "\n".join(suggested) + "\n",
        "changed_lines": changed,
    }


def render_for_workout(detail: dict[str, Any], th: dict[str, Any]) -> dict[str, Any]:
    """Render + compare one flattened/detail workout. Never raises."""
    sport = str(detail.get("sport") or "")
    sw = detail.get("structured_workout")
    if sport not in ("Bike", "Run") or not isinstance(sw, dict) or not sw.get("structure"):
        return {"renderable": False, "reason": "no structured_workout (or not Bike/Run)"}
    metric = str(sw.get("primaryIntensityMetric") or "")
    if metric == "percentOfThresholdPace" and not th.get("run_pace_sec"):
        return {"renderable": False, "reason": "run threshold pace missing in settings"}
    if metric != "percentOfThresholdPace" and not (th.get("run_ftp") if sport == "Run" else th.get("bike_ftp")):
        return {"renderable": False, "reason": "FTP missing in settings"}
    rendered = render_body_lines(sw, sport, th, str(detail.get("title") or ""))
    body, rest = split_description(detail.get("description"))
    cmp = compare_body(body, rendered)
    return {"renderable": True, "rendered_body": "\n".join(rendered) + "\n", **cmp, "explanation": rest}
