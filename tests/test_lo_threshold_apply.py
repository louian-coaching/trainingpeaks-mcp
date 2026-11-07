"""FORK 2026-11-06: lo_update_threshold_verified apply_text."""

import pytest

from tp_mcp.tools import lo_threshold as lt


def test_rewrite_watts_keeps_style():
    t, n = lt.rewrite_text("ftp_run", 270, 278, "- 3×8分鐘@243～254W\n－－\n守 243-254W，上限 ≤265 W")
    assert t == "- 3×8分鐘@250～262W\n－－\n守 250-262W，上限 ≤273 W" and n == 3


def test_rewrite_pace_scales_with_speed():
    # threshold speed 1000/236 → 1000/229 (3:56 → 3:49 /km): paces get faster
    old, new = 1000 / 236, 1000 / 229
    t, n = lt.rewrite_text("run_pace", old, new, "主段 3:56~4:02/km")
    assert t == "主段 3:49~3:55/km" and n == 1


def test_rewrite_hr_skips_out_of_range():
    t, n = lt.rewrite_text("lthr", 160, 165, "心率 ≤158，休息 60 bpm")
    assert "163" in t and "60 bpm" in t


def test_rewrite_noop_when_same():
    assert lt.rewrite_text("ftp_bike", 250, 250, "200W") == ("200W", 0)


@pytest.mark.asyncio
async def test_apply_text_write_and_render(monkeypatch):
    calls = []

    async def fake_update(wid, **f):
        calls.append((wid, f["description"]))
        return {"success": True}

    async def fake_render(wid, apply=False):
        return {"applied": False, "in_sync": True}

    import tp_mcp.tools.lo_sync as ls
    import tp_mcp.tools.lo_tools as lot
    monkeypatch.setattr(lot, "lo_update_workout_verified", fake_update)
    monkeypatch.setattr(ls, "lo_render_body", fake_render)
    stale = [{"workout_id": "9", "date": "2026-11-10", "title": "MP"}]
    by_id = {"9": {"description": "- 10公里@255~260W"}}
    res = await lt._apply_text("ftp_run", 270, 278, stale, by_id, "write")
    assert res["written"] == 1 and calls == [("9", "- 10公里@263~268W")]
    assert res["sessions"][0]["body_vs_graph"] == "in_sync"
    prev = await lt._apply_text("ftp_run", 270, 278, stale, by_id, "preview")
    assert prev["written"] == 0 and prev["sessions"][0]["new_description"] == "- 10公里@263~268W"
