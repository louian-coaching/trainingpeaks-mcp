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
