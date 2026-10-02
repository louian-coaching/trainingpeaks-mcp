"""FORK 2026-09-27: lo_sth_* StrongTri proxy — fake client, no network."""
import asyncio
import json

import pytest

from tp_mcp.tools import lo_sth


class FakeClient:
    def __init__(self, identity="C", blocking_on=None, fail_write_on=None, reorder_409_times=0):
        self.calls = []
        self.identity = identity
        self.blocking_on = blocking_on
        self.fail_write_on = fail_write_on
        self.reorder_409 = reorder_409_times
        self._next_id = 98000

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "get_current_identity":
            return {"identity": {"identityType": self.identity, "displayName": "羅譽寅"}}
        if tool == "switch_active_identity":
            self.identity = args["identity_type"]
            return {"ok": True}
        if tool == "calculate_workout_sth":
            cj = json.loads(args["classes_json"])
            assert all(s["capacity"] == "time" for st in cj.get("stages", []) for s in st["sections"])
            return {"data": {"sth": 8250.4, "stl": 50.1}}
        if tool == "create_workout":
            if args["title"] == self.blocking_on:
                return {"ok": False, "blocking": ["DURATION_TYPE_INVALID"]}
            if args.get("dry_run") is False:
                assert args["confirm_token"] == lo_sth.CONFIRM_CREATE and args["confirm_write"] is True
                if args["title"] == self.fail_write_on:
                    return {"ok": False, "blocking": ["update_failed:400000"]}
                self._next_id += 1
                return {"ok": True, "data": {"classScheduleId": self._next_id},
                        "postCreateQualityGate": {"ok": True}, "readbackOk": True,
                        "sentVsStoredDiverged": True}
            return {"ok": True, "blocking": []}
        if tool == "get_week_schedule":
            return {"days": {"2026-10-01": [{"classScheduleId": 97000, "sort": 0}]}}
        if tool == "reorder_day":
            if self.reorder_409:
                self.reorder_409 -= 1
                return {"ok": False, "error": "409001 课表正在同步设备，请稍后再操作"}
            return {"ok": True, "orderApplied": True}
        return {"ok": True}


def _payload(tmp_path, **over):
    run_cj = {"sth": 0, "stl": 0, "sportType": "RUN", "stages": [
        {"times": 1, "sections": [{"capacity": "distance", "targetSeconds": 1800}]}]}
    p = {"tri_user_id": "abc123", "workouts": [
        {"classes_date": "2026-10-01", "sport_type": "RUN", "title": "有氧耐力", "duration_min": 45,
         "classes_json": run_cj},
        {"classes_date": "2026-10-01", "sport_type": "SWIM", "title": "技術", "duration_min": 60,
         "classes_json": {"sth": 9900, "stl": 60, "sportType": "SWIM"}},
        {"classes_date": "2026-10-02", "sport_type": "RIDE", "title": "閾值間歇", "duration_min": 60,
         "classes_json": {"sth": 0, "stl": 0, "stages": [{"times": 1, "sections": [{"capacity": "time"}]}]}},
    ]}
    p.update(over)
    f = tmp_path / "week.json"
    f.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    return str(f)


def run(coro):
    return asyncio.run(coro)


def test_payload_requires_tri_user_id(tmp_path):
    f = _payload(tmp_path, tri_user_id="")
    with pytest.raises(ValueError, match="PLAT-27"):
        lo_sth.load_payload(f)


def test_calc_loads_fills_run_and_ride_and_uses_cycle(tmp_path):
    fc = FakeClient()
    f = _payload(tmp_path)
    out = run(lo_sth.lo_sth_calc_loads(f, client_factory=lambda: fc))
    p = json.loads(open(f, encoding="utf-8").read())
    assert p["workouts"][0]["classes_json"]["sth"] == 8250
    assert p["workouts"][0]["classes_json"]["stages"][0]["sections"][0]["capacity"] == "distance"  # 原檔不改
    assert p["workouts"][2]["classes_json"]["stl"] == 50
    sports = [a["sport_type"] for t, a in fc.calls if t == "calculate_workout_sth"]
    assert sports == ["RUN", "CYCLE"]
    assert out["sth_to_stl_measured"] == 165.0


def test_create_week_dry_run_does_not_write(tmp_path):
    fc = FakeClient()
    out = run(lo_sth.lo_sth_create_week(_payload(tmp_path), write=False, client_factory=lambda: fc))
    assert out["mode"] == "dry_run" and len(out["rows"]) == 3
    assert not [a for t, a in fc.calls if t == "create_workout" and a.get("dry_run") is False]


def test_create_week_blocking_stops_whole_batch(tmp_path):
    fc = FakeClient(blocking_on="技術")
    out = run(lo_sth.lo_sth_create_week(_payload(tmp_path), write=True, client_factory=lambda: fc))
    assert out["error_code"] == "DRY_RUN_BLOCKED"
    assert not [a for t, a in fc.calls if t == "create_workout" and a.get("dry_run") is False]


def test_create_week_switches_identity_and_writes(tmp_path):
    fc = FakeClient(identity="R")
    f = _payload(tmp_path)
    out = run(lo_sth.lo_sth_create_week(f, write=True, readback_save_to=str(tmp_path / "rb.json"),
                                        client_factory=lambda: fc))
    assert out["identity"]["switched"] is True
    assert out["summary"] == "3/3 堂寫入" and "isError" not in out
    assert all(a["tri_user_id"] == "abc123" and a["identity_type"] == "C"
               for t, a in fc.calls if t == "create_workout")
    assert "lo_sth_reorder_week" in out["next"]
    p = json.loads(open(f, encoding="utf-8").read())
    assert [w.get("_classScheduleId") for w in p["workouts"]] == [98001, 98002, 98003]


def test_create_week_stops_after_failed_write(tmp_path):
    fc = FakeClient(fail_write_on="技術")
    out = run(lo_sth.lo_sth_create_week(_payload(tmp_path), write=True, client_factory=lambda: fc))
    assert [r["status"] for r in out["rows"]] == ["created", "failed", "not_attempted"]
    assert out["error_code"] == "WRITE_INCOMPLETE"


def test_reorder_week_retries_409_and_keeps_existing_first(tmp_path):
    fc = FakeClient(reorder_409_times=1)
    f = _payload(tmp_path)
    run(lo_sth.lo_sth_create_week(f, write=True, client_factory=lambda: fc))
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    out = run(lo_sth.lo_sth_reorder_week(f, retry_wait_s=5, client_factory=lambda: fc, sleep=fake_sleep))
    assert out["rows"][0]["status"] == "applied" and slept == [5]
    assert out["rows"][0]["order"] == [97000, 98001, 98002]


def test_call_resolves_file_args_and_save_to(tmp_path):
    cj = tmp_path / "cj.json"
    cj.write_text('{"a":1}', encoding="utf-8")
    fc2 = FakeClient()
    res = run(lo_sth.lo_sth_call("get_week_schedule", {"note": f"@file:{cj}"}, save_to=str(tmp_path / "o.json"),
                                 client_factory=lambda: fc2))
    assert fc2.calls[0][1]["note"] == '{"a":1}'
    assert res["saved_to"].endswith("o.json") and res["ok"] is True


def test_parse_sse_and_unwrap():
    import httpx
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\\"ok\\":true}"}]}}\n\n'
    resp = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
    msg = lo_sth._parse_rpc(resp, 2)
    assert lo_sth.unwrap(msg["result"]) == {"ok": True}


def test_tools_registered_and_athlete_exempt():
    from tp_mcp.server import TOOLS
    names = {t.name: t for t in TOOLS}
    for n in lo_sth.LO_STH_TOOLS:
        assert n in names
        assert "athlete" not in names[n].input_schema["properties"]


def test_unwrap_result_json_string():
    """2026/10/02：STH 把 structuredContent.result 包成 JSON 字串，要再解一層。"""
    from tp_mcp.tools.lo_sth import unwrap
    res = {"structuredContent": {"result": '{"ok": true, "identityType": "C"}'}}
    assert unwrap(res) == {"ok": True, "identityType": "C"}
    assert unwrap({"structuredContent": {"result": "not json"}}) == {"result": "not json"}
