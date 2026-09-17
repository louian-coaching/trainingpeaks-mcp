"""Tests for tools/lo_strength.py (verified strength update + patch_exercises)."""

from __future__ import annotations

import copy
import json
from unittest.mock import MagicMock, patch

import pytest

from tp_mcp.tools import lo_strength
from tp_mcp.tools.lo_strength import fingerprint, lo_update_strength_verified

from .test_strength import _mock_tp_client


def _pv(param, val):
    return {"id": f"pv-{param}", "parameter": param, "prescribedValue": str(val), "executedValue": None,
            "inputFormat": "Integer" if param == "Reps" else "Decimal"}


def _presc(title, sets):
    return {
        "id": f"p-{title}",
        "exercise": {"id": "1", "title": title, "parameters": []},
        "parameters": [{"parameter": p, "inputFormat": "Integer"} for p in sets[0]],
        "sets": [{"id": f"s{i}", "isComplete": False, "parameterValues": [_pv(p, v) for p, v in s.items()]}
                 for i, s in enumerate(sets)],
        "coachNotes": None,
    }


def _doc():
    return {
        "id": "555", "title": "肌耐力訓練", "instructions": "維持", "prescribedDate": "2026-09-21",
        "blocks": [
            {"id": "b0", "blockType": "SingleExercise", "title": None, "coachNotes": None,
             "prescriptions": [_presc("Dumbbell Row", [{"Reps": 10, "WeightKg": 12.5}] * 3)]},
            {"id": "b1", "blockType": "SingleExercise", "title": None, "coachNotes": None,
             "prescriptions": [_presc("Dead Bug", [{"Reps": 14}] * 3)]},
        ],
        "snapshot": {"totalBlocks": 2, "totalSets": 6, "completedSets": 0},
    }


class FakeStrengthAPI:
    """GET returns the stored doc
    POST replaces it (unless `stuck`)."""

    def __init__(self, doc, stuck=False, post_status=200):
        self.doc = doc
        self.stuck = stuck
        self.post_status = post_status
        self.posted = []

    async def get(self, url, headers=None):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"data": copy.deepcopy(self.doc)}
        r.text = ""
        return r

    async def post(self, url, headers=None, json=None):
        self.posted.append(copy.deepcopy(json))
        r = MagicMock()
        r.status_code = self.post_status
        r.text = "err"
        if self.post_status == 200:
            if not self.stuck:
                self.doc = copy.deepcopy(json)
            r.json.return_value = {"data": {"id": self.doc["id"]}}
        else:
            r.json.return_value = {"errors": ["bad"]}
        return r


async def _run(api, **kw):
    with patch.object(lo_strength, "TPClient") as mtp:
        mtp.return_value.__aenter__.return_value = _mock_tp_client()
        with patch("tp_mcp.tools.lo_strength.httpx.AsyncClient") as mh:
            mh.return_value.__aenter__.return_value = api
            return await lo_update_strength_verified(**kw)


class TestPatch:
    @pytest.mark.asyncio
    async def test_set_values_changes_one_exercise_only(self):
        api = FakeStrengthAPI(_doc())
        r = await _run(
            api, workout_id="555", patch_exercises=[{"exercise": "Dumbbell Row", "set_values": {"WeightKg": 12}}]
        )
        assert r["success"] is True, r
        posted = api.posted[0]
        row = posted["blocks"][0]["prescriptions"][0]
        weights = [pv["prescribedValue"] for s in row["sets"] for pv in s["parameterValues"]
                   if pv["parameter"] == "WeightKg"]
        assert weights == ["12"] * 3
        # untouched exercise keeps its ids and values
        assert posted["blocks"][1]["prescriptions"][0]["id"] == "p-Dead Bug"
        assert r["total_sets"] == 6 and r["changed"] == ["patch Dumbbell Row"]

    @pytest.mark.asyncio
    async def test_sets_list_changes_count_and_recounts(self):
        api = FakeStrengthAPI(_doc())
        r = await _run(
            api, workout_id="555", patch_exercises=[{"exercise": "dead bug", "sets": [{"Reps": 12}, {"Reps": 12}]}]
        )
        assert r["success"] is True
        assert api.posted[0]["snapshot"]["totalSets"] == 5
        assert r["total_sets"] == 5

    @pytest.mark.asyncio
    async def test_notes_only(self):
        api = FakeStrengthAPI(_doc())
        r = await _run(api, workout_id="555", patch_exercises=[{"exercise": "Dead Bug", "notes": "慢放"}])
        assert r["success"] is True
        assert api.posted[0]["blocks"][1]["prescriptions"][0]["coachNotes"] == "慢放"

    @pytest.mark.asyncio
    async def test_unknown_exercise_and_ambiguity(self):
        api = FakeStrengthAPI(_doc())
        r = await _run(api, workout_id="555", patch_exercises=[{"exercise": "Squat", "set_values": {"Reps": 5}}])
        assert r["error_code"] == "INVALID_ARGS" and "not found" in r["message"] and api.posted == []
        doc = _doc()
        doc["blocks"][1]["prescriptions"][0]["exercise"]["title"] = "Dumbbell Row Single"
        api = FakeStrengthAPI(doc)
        r = await _run(api, workout_id="555", patch_exercises=[{"exercise": "Dumbbell Row", "set_values": {"Reps": 5}}])
        assert r["success"] is True  # exact-title match wins over substring
        api = FakeStrengthAPI(doc)
        r = await _run(api, workout_id="555", patch_exercises=[{"exercise": "Row", "set_values": {"Reps": 5}}])
        assert r["error_code"] == "INVALID_ARGS" and "block_index" in r["message"]


class TestVerify:
    @pytest.mark.asyncio
    async def test_write_not_landed_is_reported(self):
        api = FakeStrengthAPI(_doc(), stuck=True)
        r = await _run(
            api, workout_id="555", patch_exercises=[{"exercise": "Dumbbell Row", "set_values": {"WeightKg": 12}}]
        )
        assert r["success"] is False and r["error_code"] == "WRITE_NOT_LANDED"
        assert any("Dumbbell Row" in m and "sets expected" in m for m in r["mismatches"])

    @pytest.mark.asyncio
    async def test_title_and_instructions(self):
        api = FakeStrengthAPI(_doc())
        r = await _run(api, workout_id="555", title="肌耐力訓練（taper）", instructions="輕一檔")
        assert r["success"] is True and r["title"] == "肌耐力訓練（taper）"

    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self):
        api = FakeStrengthAPI(_doc())
        r = await _run(
            api, workout_id="555", dry_run=True, patch_exercises=[{"exercise": "Dead Bug", "sets": [{"Reps": 1}]}]
        )
        assert r["dry_run"] is True and api.posted == []
        assert r["expected"]["total_sets"] == 4 and r["diff_from_current"]

    @pytest.mark.asyncio
    async def test_api_rejection(self):
        api = FakeStrengthAPI(_doc(), post_status=400)
        r = await _run(api, workout_id="555", title="x")
        assert r["isError"] is True and "rejected" in r["message"]

    def test_fingerprint_normalises_numbers(self):
        from tp_mcp.tools.strength import _fmt_workout_detail

        a = fingerprint(_fmt_workout_detail(_doc()))
        d2 = _doc()
        d2["blocks"][0]["prescriptions"][0]["sets"][0]["parameterValues"][1]["prescribedValue"] = "12.50"
        b = fingerprint(_fmt_workout_detail(d2))
        assert a == b

    @pytest.mark.asyncio
    async def test_registered_and_dispatch(self):
        from tp_mcp.server import _TOOL_HANDLERS, _TOOLS_BY_NAME, call_tool

        t = _TOOLS_BY_NAME["lo_update_strength_verified"]
        assert "athlete" in t.input_schema["properties"] and "lo_update_strength_verified" in _TOOL_HANDLERS
        assert t.annotations.read_only_hint is False
        api = FakeStrengthAPI(_doc())
        with patch.object(lo_strength, "TPClient") as mtp:
            mtp.return_value.__aenter__.return_value = _mock_tp_client()
            with patch("tp_mcp.tools.lo_strength.httpx.AsyncClient") as mh:
                mh.return_value.__aenter__.return_value = api
                out = await call_tool("lo_update_strength_verified", {"workout_id": "555", "title": "T"})
        assert json.loads(out[0].text)["success"] is True
