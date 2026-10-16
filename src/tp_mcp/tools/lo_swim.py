"""FORK (2026/10/16): swim duration / TSS estimate from the workout body.

Same model as the project's tp-ai-layer/tools/swim_gen.py --css (keep the two in step):
intensity = % of CSS speed written as ``@70%`` / ``@65-70%``; lines without one count as
kick 55% / drill 60% / warm-up & cool-down 65%. Rest (``間休N秒`` × (reps-1), ``休息1~2分鐘`` = 90 s)
adds time, not load. TSS = Σ(intensity³ × seconds) / 3600 × 100. Duration is bucketed the
house way (method/1): <48→50, 48–70→60, 70–75→70, 75–85→80, ≥85→90.
"""

from __future__ import annotations

import re
from typing import Any

_SPLIT = "－－"
_PAREN = re.compile(r"[（(][^）)]*[）)]")
_MULT = re.compile(r"(\d+)\s*(?:趟|組)")
_DIST = re.compile(r"(左右各)?(\d+)\s*公尺")
_PYRA = re.compile(r"((?:\d+-)+\d+)\s*公尺")
_PCT = re.compile(r"@(\d+)(?:\s*[-~]\s*(\d+))?\s*[%％]")
_REST = re.compile(r"間休(\d+)(?:\s*[~～-]\s*(\d+))?秒")


def line_meters(line: str) -> int:
    s = line.strip()
    if not s or s == "==" or s.startswith("- 下水前｜") or "休息1~2分鐘" in s:
        return 0
    body = _PAREN.sub("", s)
    ms = _MULT.findall(body)
    mult = int(ms[-1]) if ms else 1
    total = 0
    for seq in _PYRA.findall(body):
        total += sum(int(x) for x in seq.split("-"))
    body = _PYRA.sub("", body)
    for side, d in _DIST.findall(body):
        total += int(d) * (2 if side else 1)
    return total * mult


def bucket(minutes: float) -> int:
    if minutes < 48:
        return 50
    if minutes < 70:
        return 60
    if minutes < 75:
        return 70
    if minutes < 85:
        return 80
    return 90


def estimate_swim(description: str | None, css_sec: float) -> dict[str, Any] | None:
    if not description or not css_sec:
        return None
    body = description.split(_SPLIT)[0]
    swim_s = rest_s = load = 0.0
    metres = 0
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s == "==" or s.startswith("- 下水前｜"):
            continue
        if "休息1~2分鐘" in s:
            rest_s += 90
            continue
        m = line_meters(s)
        if not m:
            continue
        metres += m
        pm = _PCT.search(s)
        if pm:
            lo = int(pm.group(1))
            hi = int(pm.group(2) or lo)
            pct = (lo + hi) / 200.0
        elif "打腿" in s:
            pct = 0.55
        elif "暖身" in s or "緩游" in s or "放鬆" in s:
            pct = 0.65
        else:
            pct = 0.60
        sec = m / 100.0 * css_sec / pct
        swim_s += sec
        load += pct ** 3 * sec
        rr = _REST.search(s)
        if rr:
            r = (int(rr.group(1)) + int(rr.group(2) or rr.group(1))) / 2.0
            n = _MULT.findall(_PAREN.sub("", s))
            reps = int(n[-1]) if n else 1
            rest_s += r * max(reps - 1, 0)
    if not metres:
        return None
    total_min = (swim_s + rest_s) / 60.0
    return {"water_min": round(total_min, 1), "duration_minutes": bucket(total_min),
            "tss_planned": round(load / 3600.0 * 100, 1), "meters": metres}
