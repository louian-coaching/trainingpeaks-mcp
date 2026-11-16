"""lo_update_threshold_verified: landed check, collateral check, stale-text scan."""

import copy
from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.tools import lo_review, settings, workouts
from tp_mcp.tools.lo_threshold import lo_update_threshold_verified, scan_stale_text


def _settings(bike_ftp=250, default_ftp=240, lthr_run=168, run_speed=1000 / 280):
    return {
        "powerZones": [
            {"workoutTypeId": 0, "threshold": default_ftp, "zones": [{"minimum": 0, "maximum": 130}]},
            {"workoutTypeId": 2, "threshold": bike_ftp, "zones": [{"minimum": 0, "maximum": 140}]},
        ],
        "heartRateZones": [
            {"workoutTypeId": 0, "threshold": 165, "zones": []},
            {"workoutTypeId": 3, "threshold": lthr_run, "zones": []},
        ],
        "speedZones": [{"workoutTypeId": 3, "threshold": run_speed, "zones": []}],
    }


class FakeSettings:
    def __init__(self, state, mutate):
        self.state = state
        self.mutate = mutate

    async def get(self):
        return {"settings": copy.deepcopy(self.state)}

    async def write(self, **kw):
        self.mutate(self.state, kw)
        return {"success": True, "threshold": kw.get("ftp")}


PLANNED = [
    {"id": 1, "date": "2026-10-05", "sport": "Bike", "type": "planned", "title": "節奏騎",
     "description": "暖身15分鐘\n3×15分鐘 @ 225~240W\n－－\n主段守住 225~240W，最後一組不超過 245W。"},
    {"id": 2, "date": "2026-10-06", "sport": "Run", "type": "planned", "title": "閾值間歇",
     "description": "4×2公里 @ 4:36~4:40/km\n－－\n心率不超過 165。"},
    {"id": 3, "date": "2026-10-07", "sport": "Bike", "type": "planned", "title": "有氧騎", "description": "90分鐘 Z2"},
]


def _wire(fs, write_name="tp_update_ftp"):
    return patch.multiple(
        settings,
        tp_get_athlete_settings=AsyncMock(side_effect=fs.get),
        **{write_name: AsyncMock(side_effect=fs.write)},
    )


def _common():
    return [
        patch.object(lo_review, "athlete_identity",
                     AsyncMock(return_value={"athlete_name": "Dominic", "athlete_id": 9})),
        patch.object(workouts, "tp_get_workouts", AsyncMock(return_value={"workouts": PLANNED})),
    ]


async def _run(fs, write_name="tp_update_ftp", **kw):
    p1, p2 = _common()
    with p1, p2, _wire(fs, write_name):
        return await lo_update_threshold_verified(today="2026-10-04", **kw)


@pytest.mark.asyncio
async def test_ftp_lands_and_lists_stale_watts():
    def mutate(st, kw):
        st["powerZones"][1]["threshold"] = kw["ftp"]
        st["powerZones"][1]["zones"] = [{"minimum": 0, "maximum": 146}]

    r = await _run(FakeSettings(_settings(), mutate), kind="ftp_bike", value=260)
    assert r["success"] is True and r["landed"] is True
    assert r["before"]["threshold"] == 250 and r["after"]["threshold"] == 260
    assert r["zones"]["before"] != r["zones"]["after"]
    stale = r["stale_text"]["workouts"]
    assert [w["workout_id"] for w in stale] == ["1"]
    m = stale[0]["matches"]
    assert m[0]["old"] == "225~240W" and m[0]["suggest"] == "234~250W" and m[0]["where"] == "body"
    assert m[-1]["old"] == "245W" and m[-1]["where"] == "explanation"


@pytest.mark.asyncio
async def test_collateral_change_on_default_set_fails():
    def mutate(st, kw):  # TECH-24 shape: the Default set moved too
        st["powerZones"][1]["threshold"] = kw["ftp"]
        st["powerZones"][0]["threshold"] = kw["ftp"]

    r = await _run(FakeSettings(_settings(), mutate), kind="ftp_bike", value=260)
    assert r["error_code"] == "COLLATERAL_CHANGE"
    assert r["collateral"][0]["workoutTypeId"] == 0


@pytest.mark.asyncio
async def test_not_landed():
    r = await _run(FakeSettings(_settings(), lambda st, kw: None), kind="ftp_bike", value=260)
    assert r["error_code"] == "WRITE_NOT_LANDED" and r["landed"] is False


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_scans():
    fs = FakeSettings(_settings(), lambda st, kw: (_ for _ in ()).throw(AssertionError("wrote")))
    r = await _run(fs, kind="run_pace", value="4:30/km", dry_run=True)
    assert "after" not in r and r["dry_run"] is True
    m = r["stale_text"]["workouts"][0]["matches"][0]
    assert m["old"] == "4:36~4:40/km"
    # threshold 4:40 -> 4:30: paces scale by 270/280
    assert m["suggest"] == "4:26~4:30/km"


@pytest.mark.asyncio
async def test_missing_sport_set_refuses_write():
    st = _settings()
    st["powerZones"] = [st["powerZones"][0]]  # no bike set
    r = await _run(FakeSettings(st, lambda s, k: None), kind="ftp_bike", value=260)
    assert r["error_code"] == "NO_SPORT_ZONE_SET"


def test_hr_scan_only_heart_rate_numbers():
    rows = scan_stale_text("lthr", 168, 172, PLANNED, ("Run",))
    assert rows[0]["matches"][0]["old"].endswith("165") and rows[0]["matches"][0]["suggest"] == "169"
    assert len(rows[0]["matches"]) == 1


def test_scan_ignores_equal_threshold():
    assert scan_stale_text("ftp_bike", 250, 250, PLANNED, ("Bike",)) == []


# ---------- FORK 2026-11-16：靜息／最大心率錨點＋補建專項區間 ----------
def _hr_state():
    st = _settings()
    st["heartRateZones"][0].update({"restingHeartRate": 185, "maximumHeartRate": 185, "calculationMethod": 24})
    return st


@pytest.mark.asyncio
async def test_anchors_only_keeps_lthr_and_verifies_resting():
    def mutate(st, kw):
        g = st["heartRateZones"][0]
        g["restingHeartRate"] = kw["resting_hr"]
        if kw.get("max_hr"):
            g["maximumHeartRate"] = kw["max_hr"]
    fs = FakeSettings(_hr_state(), mutate)
    r = await _run(fs, write_name="tp_update_hr_zones", kind="lthr", value=None, resting_hr=45, max_hr=190)
    assert r["success"] is True and r["landed"] is True and r["sent"]["threshold"] == 165
    assert fs.state["heartRateZones"][0]["restingHeartRate"] == 45


@pytest.mark.asyncio
async def test_anchor_not_landed_fails():
    fs = FakeSettings(_hr_state(), lambda st, kw: None)
    r = await _run(fs, write_name="tp_update_hr_zones", kind="lthr", value=None, resting_hr=45)
    assert r["isError"] and r["error_code"] == "WRITE_NOT_LANDED"
    assert r["anchor_mismatch"][0]["field"] == "restingHeartRate"


@pytest.mark.asyncio
async def test_value_required_unless_anchors_and_rest_below_max():
    fs = FakeSettings(_hr_state(), lambda st, kw: None)
    r = await _run(fs, kind="ftp_bike", value=None)
    assert r["error_code"] == "INVALID_ARGS"
    r = await _run(fs, kind="lthr", value=None, resting_hr=190, max_hr=185)
    assert r["error_code"] == "INVALID_ARGS"


@pytest.mark.asyncio
async def test_create_missing_bike_hr_set_with_general_method():
    st = _hr_state()
    created = {}

    async def fake_create(**kw):
        created.update(kw)
        st["heartRateZones"].append({"workoutTypeId": 2, "threshold": kw["threshold"], "calculationMethod":
                                     kw["calculation_method"], "zones": [{"minimum": 0, "maximum": 150}],
                                     "restingHeartRate": kw.get("resting_hr"), "maximumHeartRate": kw.get("max_hr")})
        return {"success": True, "created": True}
    fs = FakeSettings(st, lambda s, k: None)
    p1, p2 = _common()
    with p1, p2, patch.multiple(settings, tp_get_athlete_settings=AsyncMock(side_effect=fs.get),
                                tp_create_zones=AsyncMock(side_effect=fake_create)):
        r = await lo_update_threshold_verified(today="2026-10-04", kind="lthr", value=158,
                                               hr_workout_type="bike", create_if_missing=True)
    assert r["success"] is True and r["created"] is True and "collateral" not in r
    assert created["metric"] == "heartrate" and created["workout_type"] == "bike" and created["calculation_method"] == 3
    assert r["create"]["method_source"] == "friel default"


@pytest.mark.asyncio
async def test_missing_set_without_create_still_refuses():
    fs = FakeSettings(_hr_state(), lambda s, k: None)
    r = await _run(fs, write_name="tp_update_hr_zones", kind="lthr", value=158, hr_workout_type="bike")
    assert r["error_code"] == "NO_SPORT_ZONE_SET" and "create_if_missing" in r["message"]


@pytest.mark.asyncio
async def test_switch_method_on_existing_set_verifies_method():
    from tp_mcp.tools import lo_threshold
    st = _hr_state()
    st["heartRateZones"].append({"workoutTypeId": 2, "threshold": 160, "calculationMethod": 24,
                                 "zones": [{"minimum": 84, "maximum": 128}]})
    calls = {}

    async def fake_rewrite(key, wtid, method, thr, anchors):
        calls.update(key=key, wtid=wtid, method=method, thr=thr)
        g = st["heartRateZones"][-1]
        g["calculationMethod"] = method
        g["zones"] = [{"minimum": 0, "maximum": 130}]
        return {"success": True}
    fs = FakeSettings(st, lambda s, k: None)
    p1, p2 = _common()
    with p1, p2, patch.multiple(settings, tp_get_athlete_settings=AsyncMock(side_effect=fs.get)), \
            patch.object(lo_threshold, "_rewrite_method", AsyncMock(side_effect=fake_rewrite)):
        r = await lo_update_threshold_verified(today="2026-10-04", kind="lthr", value=160,
                                               hr_workout_type="bike", calculation_method=3)
    assert r["success"] is True and r["method_change"] == {"from": 24, "to": 3}
    assert calls == {"key": "heartRateZones", "wtid": 2, "method": 3, "thr": 160.0}
    assert "collateral" not in r


@pytest.mark.asyncio
async def test_switch_method_not_landed_fails():
    from tp_mcp.tools import lo_threshold
    st = _hr_state()
    st["heartRateZones"].append({"workoutTypeId": 2, "threshold": 160, "calculationMethod": 24, "zones": []})
    fs = FakeSettings(st, lambda s, k: None)
    p1, p2 = _common()
    with p1, p2, patch.multiple(settings, tp_get_athlete_settings=AsyncMock(side_effect=fs.get)), \
            patch.object(lo_threshold, "_rewrite_method", AsyncMock(return_value={"success": True})):
        r = await lo_update_threshold_verified(today="2026-10-04", kind="lthr", value=160,
                                               hr_workout_type="bike", calculation_method=3)
    assert r["isError"] and r["method_mismatch"] == {"sent": 3, "landed": 24}


@pytest.mark.asyncio
async def test_same_method_uses_normal_update_path():
    st = _hr_state()
    st["heartRateZones"].append({"workoutTypeId": 2, "threshold": 158, "calculationMethod": 3, "zones": []})

    def mutate(s, kw):
        s["heartRateZones"][-1]["threshold"] = kw["threshold_hr"]
    fs = FakeSettings(st, mutate)
    r = await _run(fs, write_name="tp_update_hr_zones", kind="lthr", value=160,
                   hr_workout_type="bike", calculation_method=3)
    assert r["success"] is True and "method_change" not in r


@pytest.mark.asyncio
async def test_rewrite_method_puts_full_array_with_new_method():
    from tp_mcp.tools import lo_threshold

    class R:
        def __init__(self, data):
            self.data, self.is_error, self.message, self.error_code = data, False, None, None
    groups = [{"workoutTypeId": 0, "threshold": 165, "calculationMethod": 1, "zones": []},
              {"workoutTypeId": 2, "threshold": 160, "calculationMethod": 24, "zones": [], "maximumHeartRate": 185}]
    put = {}

    class C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def ensure_athlete_id(self):
            return 9

        async def get(self, path):
            return R({"heartRateZones": copy.deepcopy(groups)})

    async def fake_calc(client, metric, g, thr, extra):
        assert metric == "heartrate" and g["calculationMethod"] == 3
        return [{"label": "Z1", "minimum": 0, "maximum": 130}], 160

    async def fake_put(client, aid, path, payload):
        put.update(aid=aid, path=path, payload=payload)
        return None
    with patch("tp_mcp.client.TPClient", C), patch.object(settings, "_calculated_zones", fake_calc), \
            patch.object(settings, "_put_zone_array", fake_put):
        r = await lo_threshold._rewrite_method("heartRateZones", 2, 3, 160.0, None)
    assert r["success"] is True and put["path"] == "heartratezones"
    assert put["payload"][0] == groups[0]
    assert put["payload"][1]["calculationMethod"] == 3 and put["payload"][1]["maximumHeartRate"] == 185
