"""羅教練 fork-only: ``lo_version`` — is the running server the code on disk?

**Context.** The fork is an editable install: a new commit is on disk the
moment it is written, but the MCP process Claude desktop started keeps running
the old modules until the app is quit (⌘Q) and reopened. That gap has bitten
four times (09/16 batch readback, 10/02 STH unwrap, 10/03 ``lo_weekly_check``
missing from the tool list…): a session calls a tool expecting the new
behaviour and silently gets the old one.

This tool answers it in one call, without running ``git`` (a ``git status``
from the Cowork VM leaves ``.git/index.lock`` behind — TECH note in
tp-mcp-fork.md §六). It reads ``.git/HEAD`` and the ref file directly, and
compares source-file mtimes with the process start time, so uncommitted edits
count too. ``stale_notice()`` is the cheap version the dispatcher attaches to
every result while the process is stale.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path
from typing import Any

_PKG_DIR = Path(__file__).resolve().parent.parent          # src/tp_mcp
_REPO_DIR = _PKG_DIR.parent.parent                          # repo root (has .git)
_STARTED_AT = time.time()


def _read_head(repo: Path = _REPO_DIR) -> dict[str, Any]:
    """{'branch', 'commit'} from .git without invoking git (no lock files)."""
    git = repo / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return {"branch": None, "commit": None}
    if not head.startswith("ref:"):
        return {"branch": None, "commit": head[:40]}
    ref = head.split(":", 1)[1].strip()
    branch = ref.rsplit("/", 1)[-1]
    try:
        return {"branch": branch, "commit": (git / ref).read_text(encoding="utf-8").strip()[:40]}
    except OSError:
        pass
    try:
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return {"branch": branch, "commit": parts[0][:40]}
    except OSError:
        pass
    return {"branch": branch, "commit": None}


def _newest_source(pkg: Path = _PKG_DIR) -> tuple[float, str | None]:
    newest, which = 0.0, None
    for p in pkg.rglob("*.py"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m > newest:
            newest, which = m, str(p.relative_to(pkg))
    return newest, which


_LOADED = _read_head()
_STALE_CACHE: dict[str, Any] = {"at": 0.0, "notice": None}


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def version_report(tool_names: list[str] | None = None) -> dict[str, Any]:
    disk = _read_head()
    newest, which = _newest_source()
    commit_moved = bool(disk.get("commit") and _LOADED.get("commit")
                        and disk["commit"] != _LOADED["commit"])
    edited_after_start = newest > _STARTED_AT + 1
    restart = commit_moved or edited_after_start
    out: dict[str, Any] = {
        "loaded_commit": (_LOADED.get("commit") or "")[:7] or None,
        "disk_commit": (disk.get("commit") or "")[:7] or None,
        "branch": disk.get("branch"),
        "process_started": _iso(_STARTED_AT),
        "newest_source_file": which,
        "newest_source_mtime": _iso(newest) if newest else None,
        "restart_needed": restart,
        "pid": os.getpid(),
    }
    if tool_names is not None:
        out["tool_count"] = len(tool_names)
        out["lo_tools"] = sorted(n for n in tool_names if n.startswith("lo_"))
    if restart:
        why = []
        if commit_moved:
            why.append(f"磁碟上已是 {out['disk_commit']}，執行中仍是 {out['loaded_commit']}")
        if edited_after_start:
            why.append(f"{which} 在程序啟動後被改過")
        out["message"] = "需要 ⌘Q 重啟 Claude desktop 才會載入新程式碼：" + "；".join(why)
    else:
        out["message"] = "執行中的程式碼就是磁碟上的版本。"
    return out


def stale_notice(ttl_s: float = 60.0) -> str | None:
    """Cheap, cached check for the dispatcher: a one-line warning while stale."""
    now = time.time()
    if now - _STALE_CACHE["at"] < ttl_s:
        return _STALE_CACHE["notice"]
    try:
        rep = version_report()
        notice = rep["message"] if rep["restart_needed"] else None
    except Exception:  # noqa: BLE001 — never let a version probe break a tool call
        notice = None
    _STALE_CACHE.update(at=now, notice=notice)
    return notice


def register_lo_version(tools: list[Any], handlers: dict[str, Any]) -> None:
    from mcp.types import Tool

    tools.append(Tool(
        name="lo_version",
        description=(
            "Is the running MCP server the code on disk? Returns loaded vs disk commit, "
            "whether any source file changed after the process started, and restart_needed. "
            "Run once at the start of a session that depends on a recent fork change; if "
            "restart_needed, the coach must quit Claude desktop (⌘Q) and reopen it. "
            "Read-only, no network, never runs git."
        ),
        input_schema={"type": "object", "properties": {}},
    ))

    async def _h(args: dict[str, Any]) -> dict[str, Any]:
        return version_report([t.name for t in tools])

    handlers["lo_version"] = _h
