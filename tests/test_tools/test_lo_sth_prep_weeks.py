"""lo_sth_prep_weeks — several STH athletes in one call, never past the bridge timeout."""

from __future__ import annotations

from tp_mcp.tools import lo_sth_ext


async def test_two_done_one_failed_one_pending(monkeypatch, tmp_path):
    clock = {"t": 0.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])

    async def fake(uid, week_start, save_dir, history_days, template_days, client_factory=None, intervals=True):
        clock["t"] += 30.0                                  # each athlete "takes" 30 s
        if uid.startswith("bad"):
            raise RuntimeError("sth down")
        return {"digest_file": f"{save_dir}/prep.json",
                "summary": {"planned_past": 8, "done": 7, "missed": ["11-15 肌力"], "last_swim": "2026-11-16",
                            "next_week_existing": [1, 2, 3]}}

    monkeypatch.setattr(lo_sth_ext, "lo_sth_prep_week", fake)
    out = await lo_sth_ext.lo_sth_prep_weeks(["aaaaaaaa1", "bad00000x", "cccccccc3", "dddddddd4"], "2026-11-16",
                                             str(tmp_path), concurrency=1, time_budget_s=55)
    rows = {r["tri_user_id"]: r for r in out["athletes"]}
    assert rows["aaaaaaaa1"]["done"] == 7 and rows["aaaaaaaa1"]["next_week_sessions"] == 3
    assert rows["aaaaaaaa1"]["digest_file"].endswith("aaaaaaaa/prep.json")
    assert rows["bad00000x"]["isError"] and "sth down" in rows["bad00000x"]["message"]
    assert out["pending"] == ["cccccccc3", "dddddddd4"] and "pending" in out["next"]


async def test_empty_list_is_rejected(tmp_path):
    out = await lo_sth_ext.lo_sth_prep_weeks([], "2026-11-16", str(tmp_path))
    assert out["error_code"] == "INVALID_ARGS"
