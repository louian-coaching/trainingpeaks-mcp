"""FORK 2026/10/16 — lo_diff_week 三個漏洞（Bullet 10/19 週同步時發現）。"""
import json

from tp_mcp.tools import lo_sync
from tp_mcp.tools.lo_review import planned_distance_m


def test_swim_parenthetical_not_counted():
    body = ("- 暖身游200公尺\n==\n"
            "- 400公尺@80-85%（不蹬牆出發、每100公尺抬頭定位1次）, 3趟, 間休30秒\n"
            "- 100公尺@85-90%, 8趟, 間休15秒\n==\n- 緩游100公尺\n－－\n說明")
    assert planned_distance_m(body) == 200 + 1200 + 800 + 100


def test_upsert_only_fields_keeps_coach_structure(tmp_path, monkeypatch):
    monkeypatch.setenv("TP_LO_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(lo_sync, "_athlete_key", lambda: "t")
    old = {"id": "1", "date": "2026-10-24", "description": "A", "structured_workout": {"s": "old"},
           "tss_planned": 120}
    snap = {"workouts": {"1": old}, "windows": [["2026-10-19", "2026-10-25"]], "updated": "x"}
    (tmp_path / "t.json").write_text(json.dumps(snap), encoding="utf-8")
    landed = {"id": "1", "date": "2026-10-24", "description": "B", "structured_workout": {"s": "coach"},
              "tss_planned": 124}
    lo_sync.upsert_snapshot_rows([landed], only_fields={"description"})
    got = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))["workouts"]["1"]
    assert got["description"] == "B"
    assert got["structured_workout"] == {"s": "old"}  # coach's structure edit still shows in the next diff
    assert got["tss_planned"] == 120
