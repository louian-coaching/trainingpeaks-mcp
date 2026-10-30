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

import bisect
import json
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# Channels we report per segment, in output order. Key = time-series key.
_CHANNELS = ("Power", "HeartRate", "Cadence", "Speed")

# Channels where a zero sample means "not pedalling / sensor dropped", not a
# real reading — averaging them in drags the number below every other tool's.
# Power is deliberately NOT here: its zeros are real (coasting), and TP's own
# average includes them (TotalWork / elapsed).
_ZERO_MEANS_MISSING = ("Cadence", "HeartRate")

_DRIFT_WARN = 0.05  # |actual/planned - 1| above this ⇒ boundaries unreliable

_ALIGN_MODES = ("auto", "laps", "shift", "elapsed", "scale")


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


_UNIT_SCALE = {"second": ("second", 1.0), "minute": ("second", 60.0), "hour": ("second", 3600.0),
               "meter": ("meter", 1.0), "kilometer": ("meter", 1000.0), "mile": ("meter", 1609.344)}


def flatten_structure(structure: list[dict[str, Any]],
                      length_unit: str | None = None) -> list[dict[str, Any]]:
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

    ``length_unit`` (``"second"`` or ``"meter"``, the structure's own axis)
    fixes mixed-unit structures. A distance run keeps time steps — "暖身跑 300
    second", "加速跑 30 second" — and TP places them on the metre axis with an
    estimated length (block begin/end), but the step value is still seconds.
    Read literally, a 30-second stride became 30 metres and every later step in
    that block slid (TECH-57: Cheng Hsun Lu's 5K test read 3:45/km instead of
    3:25). Steps in the other unit now share whatever length the block span
    leaves after the native steps. Omit it to keep the literal reading.
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
        foreign_factor = _foreign_factor(block, steps, reps, length_unit)
        for rep_i in range(reps):
            for st in steps:
                sl = st.get("length") or {}
                try:
                    val = float(sl.get("value") or 0)
                except (TypeError, ValueError):
                    val = 0.0
                if val <= 0:
                    continue
                if length_unit:
                    unit, mult = _UNIT_SCALE.get(sl.get("unit"), (length_unit, 1.0))
                    val *= mult
                    if unit != length_unit and foreign_factor:
                        val *= foreign_factor
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


def _foreign_factor(block: dict[str, Any], steps: list[dict[str, Any]], reps: int,
                    length_unit: str | None) -> float | None:
    """Axis units per foreign-unit unit for one block (see flatten_structure)."""
    if not length_unit:
        return None
    try:
        span = float(block["end"]) - float(block["begin"])
    except (KeyError, TypeError, ValueError):
        return None
    native = foreign = 0.0
    for st in steps:
        sl = st.get("length") or {}
        try:
            val = float(sl.get("value") or 0)
        except (TypeError, ValueError):
            continue
        unit, mult = _UNIT_SCALE.get(sl.get("unit"), (length_unit, 1.0))
        if unit == length_unit:
            native += val * mult
        else:
            foreign += val * mult
    remaining = span - reps * native
    if foreign <= 0 or remaining <= 0:
        return None
    return remaining / (reps * foreign)


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


def _mean_power(points: list[dict[str, Any]],
                closing: dict[str, Any] | None = None) -> tuple[float | None, str]:
    """Average power for a segment, matching TrainingPeaks' own definition.

    The charts endpoint downsamples — ~one sample per 12s on a 3.5h ride — and
    cycling power is heavily right-skewed (long stretches of coasting zeros,
    short spikes). Averaging the sampled instantaneous values therefore
    underestimates badly: on 2026/09/19 it gave 85W against TP's 135W for the
    same ride, which would have made every segment look like a failure.

    ``AccumulatedPower`` is the work counter in joules, and its final value is
    exactly TP's TotalWork — so differencing it across the segment gives the
    true average over that window regardless of how the samples were thinned.
    That is what TP means by average power (TotalWork / elapsed), zeros
    included. Fall back to the sampled mean only when the channel is absent,
    and say so in ``power_basis`` rather than quietly reporting a weaker number.

    ``closing`` is the first sample AFTER the segment. The counter has to be
    read at the segment's closing edge, not at its last interior sample, or the
    final sampling interval (≈12s) belongs to no segment at all. It feeds the
    accumulated branch only — averaging it into the sampled fallback would pull
    a work block's number towards the recovery that follows it.
    """
    acc = [
        (p["time"], p["AccumulatedPower"])
        for p in (points if closing is None else [*points, closing])
        if isinstance(p.get("AccumulatedPower"), (int, float))
        and isinstance(p.get("time"), (int, float))
    ]
    if len(acc) >= 2:
        (t0, j0), (t1, j1) = acc[0], acc[-1]
        span = t1 - t0
        if span > 0 and j1 >= j0:
            return round((j1 - j0) / span, 1), "accumulated"
    vals = [p["Power"] for p in points if isinstance(p.get("Power"), (int, float))]
    return (_mean(vals), "sampled") if vals else (None, "sampled")


def _segment_stats(points: list[dict[str, Any]],
                   closing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Averages for one segment, plus the first-half/second-half split.

    The split is what PER-24 actually asks for: a 25-minute race-power block
    that fades 12W from first half to second half was not held, even when its
    overall average lands inside the prescribed range.

    ``closing`` — the first sample after the segment — closes the
    accumulated-work window; see ``_mean_power``.
    """
    stats: dict[str, Any] = {"samples": len(points)}
    for ch in _CHANNELS:
        if ch == "Power":
            continue  # handled by _mean_power below
        vals = [p[ch] for p in points if isinstance(p.get(ch), (int, float))]
        if ch in _ZERO_MEANS_MISSING:
            vals = [v for v in vals if v > 0]
        avg = _mean(vals)
        if avg is not None:
            stats[f"avg_{ch.lower()}"] = avg

    avg_power, basis = _mean_power(points, closing)
    if avg_power is not None:
        stats["avg_power"] = avg_power
        stats["power_basis"] = basis

    # The first-half/second-half split is the PER-24 判準, so it has to use the
    # same (accumulated) basis as the average — otherwise a segment's halves
    # would be computed one way and its average another.
    if len(points) >= 4:
        mid = len(points) // 2
        # points[mid] closes the first half and opens the second, so no
        # sampling interval belongs to neither half; `closing` does the same
        # job at the segment's tail.
        first, _ = _mean_power(points[:mid], points[mid])
        second, _ = _mean_power(points[mid:], closing)
        if first is not None and second is not None:
            stats["first_half_power"] = first
            stats["second_half_power"] = second
            stats["half_split_w"] = round(second - first, 1)
    return stats


# How far the sampled mean may sit from the accumulated one before the sample
# series is too thin to answer any time-in-band question at all.
_BIAS_TOLERANCE = 0.10


def _power_bias(points: list[dict[str, Any]],
                closing: dict[str, Any] | None = None) -> float | None:
    """How far the sampled mean sits below the true (accumulated) one, as a
    fraction. None when there is no counter to check against."""
    acc, basis = _mean_power(points, closing)
    if basis != "accumulated" or not acc:
        return None
    vals = [p["Power"] for p in points if isinstance(p.get("Power"), (int, float))]
    sampled = _mean(vals)
    return None if sampled is None else abs(sampled / acc - 1.0)


def _in_range_pct(points: list[dict[str, Any]], lo: float | None, hi: float | None,
                  ftp: float | None) -> float | None:
    """Share of SAMPLES whose power sits inside the prescribed %FTP window.

    Sample-based by nature: this asks "how much of the segment was spent in the
    band", which is a time question, not a work question, so there is no
    accumulated counter that can answer it.

    That makes it only as good as the sampling. On a thinned series it is not a
    weak indication but an actively wrong one — a 25-minute block whose true
    average is 172.6W inside a 173~196W band reported 3.2%. The caller
    therefore drops it whenever the sampled mean disagrees with the accumulated
    mean by more than ``_BIAS_TOLERANCE``, and says why instead of printing a
    number that reads as a failure.
    """
    if lo is None or hi is None or not ftp:
        return None
    lo_w, hi_w = ftp * float(lo) / 100.0, ftp * float(hi) / 100.0
    vals = [p["Power"] for p in points if isinstance(p.get("Power"), (int, float))]
    if not vals:
        return None
    inside = sum(1 for v in vals if lo_w <= v <= hi_w)
    return round(100.0 * inside / len(vals), 1)


# ---------------------------------------------------------------------------
# Alignment: device laps first, then a profile shift, then raw elapsed
# (TECH-57, 2026/10/30)
# ---------------------------------------------------------------------------
#
# ``align="elapsed"`` lays the prescription's own boundaries over the clock.
# One long warm-up and every later segment lands on the wrong window: Justin
# 2026/10/22 rode his first aerobic block 1012s instead of 900s, so twelve
# intervals that his head unit split perfectly (297~306W / 267~274W /
# 240~244W, all in range) were rebuilt as 137~158W, "0% in range" — two
# weekly reviews in a row. drift_pct was only +3.2%, so no warning fired.
#
# Device laps carry ``startOffsetSeconds``/``stopOffsetSeconds`` on the same
# clock as the time series. When the athlete pressed the lap button (or the
# head unit followed the structured workout) the laps ARE the segments, so
# use them. When laps are missing or do not line up, slide the whole
# prescription along the recording and keep the offset whose power profile
# correlates best with the targets.

_LAP_EDGE_DROP_S = 15.0       # a lap this short at either end = save press / auto-pause artefact
_LAP_MAX_STEPS_PER_LAP = 3    # steps the athlete did not lap between (split proportionally)
_LAP_MAX_LAPS_PER_STEP = 12   # auto-laps inside one step (a 10 km tempo at 1 km auto-lap)
_LAP_ACCEPT_MISMATCH = 0.15   # Σ|lap − prescribed| / Σ prescribed (matched part)
_LAP_WORK_ANCHORED = 0.5     # share of work steps whose bounds come straight from laps
_LAP_MIN_COVERAGE = 0.5       # matched steps / all steps when the session stopped early
_LAP_UNMATCHED_COST = 0.5     # per unit of prescription left without a lap (selection only)

_SHIFT_BIN_S = 5
_SHIFT_RANGE_S = (-600, 900)
_SHIFT_MIN_S = 15
_SHIFT_MIN_CORR = 0.5
_SHIFT_MIN_GAIN = 0.15

_REST_CLASSES = ("warmUp", "rest", "coolDown")


def _time_to_axis(points: list[dict[str, Any]], axis: str):
    """Return f(t) mapping a clock offset to the alignment axis."""
    if axis == "time":
        return lambda t: t
    pairs = sorted(
        (float(p["time"]), v)
        for p in points
        if isinstance(p.get("time"), (int, float))
        and (v := _axis_value(p, axis)) is not None
    )
    times = [t for t, _ in pairs]

    def at(t: float) -> float | None:
        if not pairs:
            return None
        i = bisect.bisect_left(times, t)
        return pairs[min(i, len(pairs) - 1)][1]
    return at


def device_laps(raw: dict[str, Any], points: list[dict[str, Any]],
                axis: str) -> list[dict[str, Any]]:
    """Device laps as ``[{n, start, end}]`` on the alignment axis.

    ``n`` is the 1-based lap number as the head unit wrote it. Laps without
    offsets make the whole list unusable (a partial list would mis-number
    everything after the gap).
    """
    laps: list[tuple[float, float, int]] = []
    numbered = 0
    for lap in raw.get("lapData") or []:
        if not isinstance(lap, dict):
            continue
        # TP adds its own "DetectedPaceInterval" laps that overlap the device
        # laps (a 5 km "Pace Interval 3" on top of five 1 km laps); only what
        # the head unit wrote describes how the athlete split the session.
        if lap.get("Source") not in (None, "Device"):
            continue
        numbered += 1
        s, e = lap.get("startOffsetSeconds"), lap.get("stopOffsetSeconds")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)) or e <= s:
            return []
        laps.append((float(s), float(e), numbered))
    laps.sort()
    kept: list[tuple[float, float, int]] = []
    for lap in laps:
        if kept and lap[0] < kept[-1][1] - 1.0:
            continue  # overlaps the previous lap — not part of the device sequence
        kept.append(lap)
    laps = kept
    while laps and laps[-1][1] - laps[-1][0] < _LAP_EDGE_DROP_S:
        laps.pop()
    while laps and laps[0][1] - laps[0][0] < _LAP_EDGE_DROP_S:
        laps.pop(0)
    conv = _time_to_axis(points, axis)
    out = []
    for s, e, n in laps:
        a, b = conv(s), conv(e)
        if a is None or b is None or b <= a:
            return []
        out.append({"n": n, "start": float(a), "end": float(b)})
    return out


def align_to_laps(segs: list[dict[str, Any]], laps: list[dict[str, Any]],
                  axis: str = "time") -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Map prescription steps onto device laps (monotone, with merges).

    A lap may cover up to three consecutive steps (athlete did not press
    between them — split proportionally to the prescription), and a step may
    span up to twelve laps (extra presses, or 1 km auto-laps inside a long
    block). Dynamic
    programming minimises Σ|lap duration − step duration| plus a small
    penalty per merge so 1:1 wins ties. The run must consume every lap (a
    session cut short leaves trailing steps unmatched) or every step (extra
    laps after the last step are ignored).

    Returns ``(segments, info)``; segments is None when the laps do not line
    up well enough to trust, and ``info["reason"]`` says why.
    """
    n, m = len(laps), len(segs)
    info: dict[str, Any] = {"device_laps": n, "prescribed_steps": m}
    if n < 2 or m < 1:
        info["reason"] = "fewer than two usable device laps"
        return None, info
    ld = [lp["end"] - lp["start"] for lp in laps]
    sd = [s["end"] - s["start"] for s in segs]
    pen = 5.0 if axis == "time" else 20.0
    inf = float("inf")
    best = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    best[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            cur = best[i][j]
            if cur == inf:
                continue
            for a in range(1, _LAP_MAX_LAPS_PER_STEP + 1):
                if i + a > n:
                    break
                for b in range(1, _LAP_MAX_STEPS_PER_LAP + 1):
                    if j + b > m:
                        break
                    if a > 1 and b > 1:
                        break
                    c = abs(sum(ld[i:i + a]) - sum(sd[j:j + b])) + pen * (a + b - 2)
                    if cur + c < best[i + a][j + b]:
                        best[i + a][j + b] = cur + c
                        back[i + a][j + b] = (a, b)
    ends = [(n, j) for j in range(1, m + 1)] + [(i, m) for i in range(1, n)]
    cands = []
    for i, j in ends:
        if best[i][j] == inf:
            continue
        matched = sum(sd[:j])
        if matched <= 0 or (j < m and j / m < _LAP_MIN_COVERAGE):
            continue
        # Leaving steps unmatched must cost something, or the cheapest answer
        # is to swallow the cool-down laps into the last work block (Cheng
        # Hsun Lu 10/22: 5K test + walking cool-down read as 4:13/km).
        unmatched = sum(sd[j:])
        cands.append((best[i][j] + _LAP_UNMATCHED_COST * unmatched, -j, i, j, matched))
    if not cands:
        info["reason"] = "laps cannot be paired with the prescription"
        return None, info
    _, _, i, j, matched_len = min(cands)
    norm = best[i][j] / matched_len
    groups = []
    while i > 0 or j > 0:
        a, b = back[i][j]
        groups.append((i - a, a, j - b, b))
        i, j = i - a, j - b
    groups.reverse()

    out: list[dict[str, Any]] = []
    work_total = work_anchored = 0
    for li, a, sj, b in groups:
        lap_lo, lap_hi = laps[li], laps[li + a - 1]
        if a == 1 and b == 1:
            lap_label = f"Lap {lap_lo['n']}"
        elif b == 1:
            lap_label = f"Lap {lap_lo['n']}–{lap_hi['n']}"
        else:
            lap_label = f"Lap {lap_lo['n']}（{b} 段合一圈，依處方比例切分）"
        start, end = lap_lo["start"], lap_hi["end"]
        span_pres = sum(sd[sj:sj + b]) or 1.0
        cursor = start
        for k in range(sj, sj + b):
            seg = segs[k]
            seg_end = end if k == sj + b - 1 else cursor + (end - start) * sd[k] / span_pres
            out.append({**seg, "start": cursor, "end": seg_end, "lap": lap_label})
            cursor = seg_end
            if seg.get("intensity_class") not in _REST_CLASSES:
                work_total += 1
                if b == 1:
                    work_anchored += 1
    info["mismatch_pct"] = round(100.0 * norm, 1)
    info["merged_groups"] = sum(1 for g in groups if g[1] > 1 or g[3] > 1)
    if work_total:
        info["work_steps_on_lap_bounds"] = f"{work_anchored}/{work_total}"
    if norm > _LAP_ACCEPT_MISMATCH:
        info["reason"] = (f"lap durations differ from the prescription by "
                          f"{100 * norm:.0f}% (> {100 * _LAP_ACCEPT_MISMATCH:.0f}%)")
        return None, info
    if work_total and work_anchored / work_total < _LAP_WORK_ANCHORED:
        info["reason"] = "too few work steps start and end on a lap boundary"
        return None, info
    matched = len(out)
    if matched < m:
        info["unmatched_steps"] = [s["name"] for s in segs[matched:]]
    return out, info


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


def best_shift(points: list[dict[str, Any]], segs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Slide the prescription along the recording (time axis only).

    Score = Pearson correlation between the target midpoints and the 5-second
    mean of Power (Speed when there is no power). The prescription must stay
    ≥80% inside the recording for a shift to count. Returns None when there
    is nothing to correlate (flat targets, flat signal, too few samples).
    """
    ch = "Power" if any(isinstance(p.get("Power"), (int, float)) for p in points) else "Speed"
    bins: dict[int, list[float]] = {}
    for p in points:
        t, v = p.get("time"), p.get(ch)
        if isinstance(t, (int, float)) and isinstance(v, (int, float)):
            bins.setdefault(int(t // _SHIFT_BIN_S), []).append(float(v))
    act = {k: sum(v) / len(v) for k, v in bins.items()}
    tgt: dict[int, float] = {}
    for s in segs:
        lo, hi = s.get("target_min"), s.get("target_max")
        if lo is None or hi is None:
            continue
        try:
            mid = (float(lo) + float(hi)) / 2.0
        except (TypeError, ValueError):
            continue
        for k in range(int(s["start"] // _SHIFT_BIN_S), int(math.ceil(s["end"] / _SHIFT_BIN_S))):
            tgt[k] = mid
    if len(tgt) < 6 or len(act) < 6:
        return None

    def corr(sb: int) -> float | None:
        xs, ys = [], []
        for k, mid in tgt.items():
            a = act.get(k + sb)
            if a is not None:
                xs.append(mid)
                ys.append(a)
        if len(xs) < 0.8 * len(tgt):
            return None
        return _pearson(xs, ys)

    c0 = corr(0)
    lo_b, hi_b = _SHIFT_RANGE_S[0] // _SHIFT_BIN_S, _SHIFT_RANGE_S[1] // _SHIFT_BIN_S
    top: tuple[float, int] | None = None
    for sb in sorted(range(lo_b, hi_b + 1), key=abs):
        c = corr(sb)
        if c is not None and (top is None or c > top[0] + 1e-9):
            top = (c, sb)
    if top is None:
        return None
    return {
        "shift_s": top[1] * _SHIFT_BIN_S,
        "corr": round(top[0], 3),
        "corr_unshifted": round(c0, 3) if c0 is not None else None,
        "channel": ch,
    }


def _shift_is_convincing(sh: dict[str, Any] | None) -> bool:
    if not sh or abs(sh["shift_s"]) < _SHIFT_MIN_S or sh["corr"] < _SHIFT_MIN_CORR:
        return False
    c0 = sh.get("corr_unshifted")
    return c0 is None or sh["corr"] - c0 >= _SHIFT_MIN_GAIN


async def lo_verify_intervals(
    workout_id: str,
    segments: list[dict[str, Any]] | None = None,
    align: str = "auto",
    ftp: float | None = None,
    include_rest: bool = False,
) -> dict[str, Any]:
    """Reconstruct per-segment averages from the time series, without laps.

    Args:
        workout_id: completed workout to verify.
        segments: optional explicit ``[{name, start_s, end_s, target_min,
            target_max}]``. Omit to derive them from the workout's own
            ``structured_workout``.
        align: ``auto`` (default: device laps when they line up with the
            prescription, else a whole-prescription shift when the power
            profile clearly says so, else raw elapsed), ``laps``, ``shift``,
            ``elapsed`` (prescribed boundaries as-is) or ``scale`` (stretch
            the prescription onto actual elapsed time).
        ftp: FTP/threshold used for the in-range percentage. Omit to skip it.
        include_rest: also report warm-up/rest/cool-down segments.
    """
    from tp_mcp.tools.analyze import tp_analyze_workout
    from tp_mcp.tools.workouts import tp_get_workout

    if align not in _ALIGN_MODES:
        return _err("INVALID_ARGS", f"align must be one of {', '.join(_ALIGN_MODES)}")

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
        segs = flatten_structure(structure, "meter" if axis == "distance" else "second")
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

    used = align
    lap_info: dict[str, Any] | None = None
    shift_info: dict[str, Any] | None = None
    if align in ("auto", "laps"):
        if source == "manual":
            if align == "laps":
                return _err("INVALID_ARGS",
                            "align='laps' maps the workout's own structure onto device laps; "
                            "it cannot be combined with manual segments.")
            used = "elapsed"
        else:
            mapped, lap_info = align_to_laps(segs, device_laps(raw, points, axis), axis)
            if mapped is not None:
                segs, used = mapped, "laps"
            elif align == "laps":
                return _err(
                    "LAPS_UNMATCHED",
                    f"Device laps do not line up with the prescription "
                    f"({lap_info.get('reason')}). Use align='auto'.",
                )
    if used == "auto" or align == "shift":
        shift_info = best_shift(points, segs) if axis == "time" else None
        if align == "shift" and not shift_info:
            return _err("SHIFT_UNAVAILABLE",
                        "No shift can be scored (distance axis, flat targets or flat signal).")
        if align == "shift" or _shift_is_convincing(shift_info):
            off = shift_info["shift_s"]
            segs = [{**s, "start": s["start"] + off, "end": s["end"] + off} for s in segs]
            used = "shift"
        else:
            used = "elapsed"
    if align == "scale" and planned_total:
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
        if seg.get("lap"):
            row["lap"] = seg["lap"]
        if seg.get("target_min") is not None:
            row["target_pct"] = [seg["target_min"], seg["target_max"]]
            if ftp:
                row["target_w"] = [
                    _round_half_up(ftp * float(seg["target_min"]) / 100.0),
                    _round_half_up(ftp * float(seg["target_max"]) / 100.0),
                ]
        after = [
            (v, p) for p in points
            if (v := _axis_value(p, axis)) is not None and v >= seg["end"]
        ]
        closing = min(after, key=lambda vp: vp[0])[1] if after else None
        row.update(_segment_stats(in_seg, closing))
        pct = _in_range_pct(in_seg, seg.get("target_min"), seg.get("target_max"), ftp)
        if pct is not None:
            bias = _power_bias(in_seg, closing)
            if bias is None or bias <= _BIAS_TOLERANCE:
                row["in_range_pct"] = pct
            else:
                row["in_range_note"] = (
                    f"in_range_pct withheld: the sampled series sits {bias * 100:.0f}% "
                    f"off the accumulated average, so it cannot say how much of this "
                    f"segment was spent in the band. Judge by avg_power."
                )
        rows.append(row)

    out: dict[str, Any] = {
        "success": True,
        "workout_id": workout_id,
        "segment_source": source,
        "align": used,
        "align_requested": align,
        "axis": axis,
        "segments": rows,
        "time_series_points": len(points),
    }
    if lap_info is not None:
        out["lap_match"] = lap_info
        if lap_info.get("unmatched_steps"):
            out["unmatched_note"] = (
                "The device laps end before the prescription does — these steps have no "
                "recording. Check the athlete's comment before calling them skipped."
            )
    if used == "laps":
        out["alignment_note"] = "Segments use the device lap boundaries (lap column)."
    elif used == "shift" and shift_info:
        out["shift"] = shift_info
        out["alignment_note"] = (
            f"No usable device laps; the whole prescription was shifted "
            f"{shift_info['shift_s']:+d}s to match the {shift_info['channel']} profile "
            f"(correlation {shift_info['corr_unshifted']} -> {shift_info['corr']}). "
            f"A step the athlete lengthened or cut still skews the segments after it."
        )
    if drift_pct is not None:
        out["drift_pct"] = drift_pct
        if used == "elapsed" and abs(drift_pct) > _DRIFT_WARN * 100:
            out["warning"] = (
                f"Actual elapsed is {drift_pct:+.1f}% vs the prescription, so segment "
                f"boundaries drift by up to {abs(actual_total - planned_total):.0f}"
                f"{'s' if axis == 'time' else 'm'} by the end of the session. "
                f"Re-run with align='scale' if the athlete simply went long/short "
                f"uniformly; treat late segments as approximate otherwise."
            )
    # The compact analysis (the default since 3c2d641) has no ``lapData`` —
    # counting it reported "single lap" for every workout. Use the flag the
    # analysis itself computes, falling back to counting either lap key.
    single = analysis.get("single_lap")
    if single is None:
        single = len(analysis.get("lapData") or analysis.get("laps") or []) <= 1
    if single:
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
            "Default align='auto' puts each step on the device lap that matches it "
            "when the laps line up (a long warm-up no longer shifts every later "
            "interval), otherwise slides the whole prescription to the offset whose "
            "power profile fits best, otherwise uses raw elapsed time; the response "
            "says which (`align`, `lap` per segment, `lap_match`, `shift`). Reports "
            "drift between prescribed and actual elapsed time; pass align='scale' "
            "when the session ran uniformly long or short, or supply `segments` "
            "explicitly to check arbitrary windows."
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
                    "enum": list(_ALIGN_MODES),
                    "default": "auto",
                    "description": (
                        "auto = device laps if they line up, else profile shift if "
                        "convincing, else elapsed; laps = device laps or error; shift = "
                        "best whole-prescription offset; elapsed = prescribed boundaries "
                        "as-is; scale = stretch them onto actual elapsed time."
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
            align=a.get("align", "auto"),
            ftp=a.get("ftp"),
            include_rest=bool(a.get("include_rest", False)),
        )

    handlers["lo_verify_intervals"] = _h_verify
