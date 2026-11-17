"""2026/11/17 開發迴路：錯誤要找得到原因、分析檔要放在 session 讀得到的地方。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tp_mcp.server as srv
from tp_mcp.server import call_tool
from tp_mcp.tools import analyze


async def test_internal_error_names_the_class_and_logs_the_traceback(monkeypatch, tmp_path):
    (tmp_path / "trainingpeaks-mcp" / "_scratch").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    async def boom(args):
        raise KeyError("cookie=" + "A" * 60)

    monkeypatch.setitem(srv._TOOL_HANDLERS, "tp_auth_status", boom)
    payload = json.loads((await call_tool("tp_auth_status", {}))[0].text)
    assert payload["error_type"] == "KeyError" and "KeyError" in payload["message"]
    assert "AAAA" not in payload["message"]                      # the text itself stays out
    log = Path(payload["error_log"]).read_text(encoding="utf-8")
    assert "tp_auth_status" in log and "KeyError" in log and "A" * 40 not in log   # redacted


def test_analysis_dir_prefers_scratch_and_prunes(monkeypatch, tmp_path):
    monkeypatch.delenv("TP_MCP_ANALYSIS_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert "_scratch" not in str(analyze._analysis_dir())        # no repo here: temp dir
    (tmp_path / "trainingpeaks-mcp" / "_scratch").mkdir(parents=True)
    assert analyze._analysis_dir() == tmp_path / "trainingpeaks-mcp" / "_scratch" / "analysis"
    d = tmp_path / "dumps"
    d.mkdir()
    for i in range(5):
        (d / f"workout_{i}.json").write_text("{}")
    analyze._prune(d, keep=2)
    assert len(list(d.glob("workout_*.json"))) == 2
