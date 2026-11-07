"""FORK 2026-11-06 (coach menu): draft the explanation update after a coach edit.

Context. 11/03–11/06 the coach edited ~15 planned weeks in the TP calendar; each
PROC-80 sync meant re-typing the numbers in the explanation (below ``－－``) by
hand: 「235~248W」→「230~242W」, 「間休 20 秒」→「30 秒」, 「比賽後第 2 天」→「第 3 天」
after a date move. ``lo_diff_week`` already listed the stale numbers; this module
turns them into a *draft* — it never writes. The model reviews the draft, fixes
what the tool could not pair (``unresolved``), and writes with
``lo_update_workout_verified``; R80 in validate_week still checks the result.

Pure functions only (unit-tested without TP).
"""

from __future__ import annotations

import difflib
import re
from datetime import date
from typing import Any

# Same notion of a "number with meaning" as lo_sync._NUM_TOKEN: a unit, a colon, or a range.
_TOKEN = re.compile(
    r"(?<![\d.:])\d+(?:[.:]\d+)?(?:\s*[~～\-]\s*\d+(?:[.:]\d+)?)?"
    r"(?:\s*(?:公里|公尺|分鐘|秒|W|/km|/100m|km|k|組|趟|%|bpm|rpm|克))?"
)
_RANGE_SEP = re.compile(r"\s*[~～\-]\s*")


def _norm(tok: str) -> str:
    return _RANGE_SEP.sub("~", re.sub(r"\s+", "", tok))


def _tokens(line: str) -> list[str]:
    out = []
    for m in _TOKEN.finditer(line or ""):
        t = _norm(m.group(0))
        if re.search(r"[:~]|[^\d.]$", t):
            out.append(t)
    return out


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def number_substitutions(old_body: str, new_body: str) -> list[tuple[str, str]]:
    """Old→new number tokens, paired by position on lines that changed.

    Lines are aligned with difflib; within a replaced line pair the token lists are
    aligned again, and only 1:1 replacements are kept (an inserted or deleted token
    has no partner and is left to the human). A token that maps to two different
    new values is ambiguous and dropped.
    """
    a, b = _lines(old_body), _lines(new_body)
    pairs: dict[str, set[str]] = {}
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace":
            continue
        for la, lb in zip(a[i1:i2], b[j1:j2], strict=False):
            ta, tb = _tokens(la), _tokens(lb)
            tsm = difflib.SequenceMatcher(a=ta, b=tb, autojunk=False)
            for top, x1, x2, y1, y2 in tsm.get_opcodes():
                if top == "replace" and (x2 - x1) == (y2 - y1):
                    for o, n in zip(ta[x1:x2], tb[y1:y2], strict=True):
                        if o != n:
                            pairs.setdefault(o, set()).add(n)
    return sorted((o, next(iter(ns))) for o, ns in pairs.items() if len(ns) == 1)


def _token_regex(tok: str) -> re.Pattern[str]:
    parts = []
    for ch in tok:
        if ch == "~":
            parts.append(r"\s*[~～\-]\s*")
        else:
            parts.append(re.escape(ch))
    unit_tail = r"(?![A-Za-z])" if re.search(r"[A-Za-z]$", tok) else ""
    return re.compile(r"(?<![\d.:])" + r"\s*".join(parts) + r"(?![\d.:])" + unit_tail)


def _swap(found: str, new: str) -> str:
    """Write ``new`` in the explanation's own range style (～ / ~ / -)."""
    sep = re.search(r"[~～\-]", found)
    return new.replace("~", sep.group(0)) if sep else new


def _ctx(text: str, m: re.Match[str], width: int = 12) -> str:
    return text[max(0, m.start() - width):m.end() + width].replace("\n", " ")


_POST_RACE = re.compile(r"((?:比賽|賽)(?:後|完)第\s*)(\d+)(\s*天)")
_D_PLUS = re.compile(r"(D\s*\+\s*)(\d+)")
_D_MINUS = re.compile(r"(D\s*[-−]\s*)(\d+)")
_WEEKDAY = re.compile(r"(?:週|星期|禮拜)[一二三四五六日天]")


def _days(a: str, b: str) -> int | None:
    try:
        return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days
    except (TypeError, ValueError):
        return None


def draft_explanation(old_body: str, new_body: str, explanation: str,
                      date_from: str | None = None, date_to: str | None = None,
                      stale_numbers: list[str] | None = None) -> dict[str, Any]:
    """Return {draft, replacements, unresolved, check_phrases}. Never raises."""
    draft = explanation or ""
    reps: list[dict[str, str]] = []
    for old, new in number_substitutions(old_body, new_body):
        rx = _token_regex(old)
        hits = list(rx.finditer(draft))
        if not hits:
            continue
        for m in hits:
            reps.append({"from": m.group(0), "to": _swap(m.group(0), new), "context": _ctx(draft, m)})
        draft = rx.sub(lambda m, new=new: _swap(m.group(0), new), draft)

    delta = _days(date_from, date_to) if date_from and date_to else None
    if delta:
        def _shift(sign: int):
            def f(m: re.Match[str]) -> str:
                n = int(m.group(2)) + sign * delta
                rep = f"{m.group(1)}{n}{m.group(3) if m.lastindex and m.lastindex >= 3 else ''}"
                if n >= 0:
                    reps.append({"from": m.group(0), "to": rep, "context": f"日期移動 {delta:+d} 天"})
                    return rep
                return m.group(0)
            return f
        draft = _POST_RACE.sub(_shift(+1), draft)
        draft = _D_PLUS.sub(_shift(+1), draft)
        draft = _D_MINUS.sub(_shift(-1), draft)

    norm_draft = _norm(draft)
    unresolved = sorted({s for s in (stale_numbers or []) if _norm(s) in norm_draft})
    check = []
    if delta:
        check = sorted(set(_WEEKDAY.findall(draft)))
    return {"draft": draft, "replacements": reps, "unresolved": unresolved,
            "check_phrases": check, "changed": draft != (explanation or "")}


def compose_description(body: str, explanation: str, original_body: str | None = None) -> str:
    """Body + explanation, keeping the original whitespace before ``－－``."""
    tail = re.search(r"\s*$", original_body or "").group(0) or "\n"
    return body.rstrip() + tail + explanation.lstrip() if explanation else body
