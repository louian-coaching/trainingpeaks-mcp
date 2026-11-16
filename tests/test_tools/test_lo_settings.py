"""FORK 2026-11-16: lo_settings_audit（純函式）＋ lo_set_pool_length（假 client）。"""
from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.client.http import APIResponse
from tp_mcp.tools import lo_review, lo_settings
from tp_mcp.tools.lo_settings import audit_settings, lo_set_pool_length

ZHAO = {
    "heartRateZones": [{"workoutTypeId": 0, "threshold": 178, "restingHeartRate": 185, "maximumHeartRate": 185},
                       {"workoutTypeId": 3, "threshold": 182, "restingHeartRate": 0, "maximumHeartRate": 0}],
    "powerZones": [{"workoutTypeId": 0, "threshold": 200}],
    "speedZones": [{"workoutTypeId": 0, "threshold": 1000 / 275}, {"workoutTypeId": 3, "threshold": 1000 / 270}],
}
POOL_25Y = {"options": [{"id": "DEFAULT-25Y", "length": 25.0, "units": "Yards"},
                        {"id": "DEFAULT-25M", "length": 25.0, "units": "Meters"},
                        {"id": "DEFAULT-50M", "length": 50.0, "units": "Meters"}], "defaultId": "DEFAULT-25Y"}


def test_audit_flags_resting_hr_general_drift_pool_and_basic():
    r = audit_settings(ZHAO, POOL_25Y, {"tier": "basic"})
    codes = {f["code"] for f in r["flags"]}
    assert {"HR_RESTING_GE_MAX", "POOL_DEFAULT_YARDS", "ACCOUNT_BASIC"} <= codes
    assert [i["code"] for i in r["info"]] == ["GENERAL_DIFFERS"]   # run speed only; swim not compared
    assert "general" in r["sets"]["heartRateZones"] and "run" in r["sets"]["heartRateZones"]


def test_audit_clean_settings_no_flags():
    ok = {"heartRateZones": [{"workoutTypeId": 0, "threshold": 170, "restingHeartRate": 45, "maximumHeartRate": 190}],
          "powerZones": [{"workoutTypeId": 0, "threshold": 250}, {"workoutTypeId": 2, "threshold": 252}]}
    pool = {**POOL_25Y, "defaultId": "DEFAULT-25M"}
    r = audit_settings(ok, pool, {"tier": "premium_coach"})
    assert r["flags"] == [] and r["info"] == []


class FakeClient:
    def __init__(self, pool, post_returns="opt-1"):
        self.pool = pool
        self.post_returns = post_returns
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def ensure_athlete_id(self):
        return 77

    async def get(self, ep, params=None):
        self.calls.append(("GET", ep))
        return APIResponse(success=True, data=self.pool)

    async def post(self, ep, json=None):
        self.calls.append(("POST", ep, json))
        opt = {"id": self.post_returns or "opt-x", "length": json["length"], "units": json["units"]}
        self.pool["options"].append(opt)
        return APIResponse(success=True, data=self.post_returns)

    async def put(self, ep, json=None):
        self.calls.append(("PUT", ep))
        self.pool["defaultId"] = ep.rsplit("/", 1)[1]
        return APIResponse(success=True, data=None)


async def _run(fc, **kw):
    ident = AsyncMock(return_value={"athlete_id": 77, "athlete_name": "Nathan"})
    with patch.object(lo_settings, "TPClient", lambda: fc), \
         patch.object(lo_review, "athlete_identity", ident):
        return await lo_set_pool_length(**kw)


@pytest.mark.asyncio
async def test_pool_switches_to_existing_25m_option():
    import copy
    fc = FakeClient(copy.deepcopy(POOL_25Y))
    r = await _run(fc, length=25)
    assert r["success"] and r["after"]["defaultId"] == "DEFAULT-25M"
    assert ("PUT", "/fitness/v1/athletes/77/poollengthsettings/defaultoption/DEFAULT-25M") in fc.calls
    assert not any(c[0] == "POST" for c in fc.calls)


@pytest.mark.asyncio
async def test_pool_creates_custom_option_when_missing():
    import copy
    fc = FakeClient(copy.deepcopy(POOL_25Y), post_returns=None)
    r = await _run(fc, length=33.3)
    assert r["success"] and r["created_option"] == "opt-x" and r["after"]["default"]["length"] == 33.3


@pytest.mark.asyncio
async def test_pool_no_change_and_dry_run_and_guards():
    import copy
    fc = FakeClient({**copy.deepcopy(POOL_25Y), "defaultId": "DEFAULT-50M"})
    assert (await _run(fc, length=50))["status"] == "no_change"
    r = await _run(fc, length=25, dry_run=True)
    assert r["success"] and not any(c[0] == "PUT" for c in fc.calls)
    assert (await _run(fc, length=25, units="feet"))["error_code"] == "INVALID_ARGS"
    assert (await _run(fc, length=5))["error_code"] == "INVALID_ARGS"
