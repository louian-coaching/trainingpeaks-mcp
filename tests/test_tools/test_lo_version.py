"""lo_version: loaded vs disk commit, edits after start, no git invocation."""

import time
from pathlib import Path

import pytest

from tp_mcp.tools import lo_version as lv


def _fake_repo(tmp_path: Path, commit: str, packed: bool = False) -> Path:
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    if packed:
        (git / "packed-refs").write_text(f"# pack-refs\n{commit} refs/heads/main\n")
    else:
        (git / "refs" / "heads" / "main").write_text(commit + "\n")
    return tmp_path


def test_read_head_loose_and_packed(tmp_path):
    c = "a" * 40
    assert lv._read_head(_fake_repo(tmp_path / "x", c)) == {"branch": "main", "commit": c}
    assert lv._read_head(_fake_repo(tmp_path / "y", c, packed=True)) == {"branch": "main", "commit": c}


def test_read_head_detached_and_missing(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("b" * 40)
    assert lv._read_head(tmp_path)["commit"] == "b" * 40
    assert lv._read_head(tmp_path / "nope") == {"branch": None, "commit": None}


def test_report_fresh(monkeypatch):
    monkeypatch.setattr(lv, "_LOADED", {"branch": "main", "commit": "c" * 40})
    monkeypatch.setattr(lv, "_read_head", lambda *a: {"branch": "main", "commit": "c" * 40})
    monkeypatch.setattr(lv, "_newest_source", lambda *a: (lv._STARTED_AT - 100, "x.py"))
    rep = lv.version_report(["lo_a", "tp_b"])
    assert rep["restart_needed"] is False
    assert rep["lo_tools"] == ["lo_a"] and rep["tool_count"] == 2


def test_report_commit_moved(monkeypatch):
    monkeypatch.setattr(lv, "_LOADED", {"branch": "main", "commit": "c" * 40})
    monkeypatch.setattr(lv, "_read_head", lambda *a: {"branch": "main", "commit": "d" * 40})
    monkeypatch.setattr(lv, "_newest_source", lambda *a: (lv._STARTED_AT - 100, "x.py"))
    rep = lv.version_report()
    assert rep["restart_needed"] is True
    assert "ddddddd" in rep["message"] and "⌘Q" in rep["message"]


def test_report_uncommitted_edit(monkeypatch):
    monkeypatch.setattr(lv, "_LOADED", {"branch": "main", "commit": "c" * 40})
    monkeypatch.setattr(lv, "_read_head", lambda *a: {"branch": "main", "commit": "c" * 40})
    monkeypatch.setattr(lv, "_newest_source", lambda *a: (time.time() + 10, "tools/x.py"))
    rep = lv.version_report()
    assert rep["restart_needed"] is True and "tools/x.py" in rep["message"]


def test_stale_notice_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(lv, "version_report",
                        lambda *a: calls.append(1) or {"restart_needed": True, "message": "m"})
    monkeypatch.setattr(lv, "_STALE_CACHE", {"at": 0.0, "notice": None})
    assert lv.stale_notice() == "m"
    assert lv.stale_notice() == "m"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_registered_and_exempt():
    from tp_mcp import server
    names = [t.name for t in server.TOOLS]
    assert "lo_version" in names
    tool = server._TOOLS_BY_NAME["lo_version"]
    assert "athlete" not in tool.input_schema["properties"]
    assert tool.annotations.read_only_hint is True
