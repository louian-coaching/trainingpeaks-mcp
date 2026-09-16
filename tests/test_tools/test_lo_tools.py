"""Tests for tools/lo_tools.py (羅教練 fork-only tools)."""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.tools import lo_tools
from tp_mcp.tools.lo_tools import lo_set_sport, lo_update_workout_verified, normalize_aliases

# ---------------------------------------------------------------------------
# A fake TP: tp_get_workout reads from a dict, tp_update_workout mutates it —
# unless `stuck` is set, which reproduces "success: true but nothing landed".
# ---------------------------------------------------------------------------

_SW = {
    "primaryLengthMetric": "duration",
    "primaryIntensityMetric": "percentOfFtp",
    "structure": [
        {"begin": 0, "end": 600, "steps": [{"name": "WU"}]},
        {"begin": 600, "end": 1800, "steps": [{"name": "Main"}]},
    ],
    "polyline": [[0, 50], [600, 50], [600, 90], [1800, 90]],
}


def _detail(**over):
    d = {
        "id": "1001",
        "date": "2026-09-25T00:00:00",
        "title": "再试试",
        "sport": "Bike",
        "workout_type": 2,
        "description": None,
        "metrics": {"duration_planned": 4.75, "tss_planned": None, "distance_planned_km": None},
        "completed": False,
        "structured_workout": None,
    }
    d.update(over)
    return d


class FakeTP:
    def __init__(self, detail, stuck: set[str] | None = None, drift_sport: str | None = None, fail=None):
        self.state = copy.deepcopy(detail)
        self.stuck = stuck or set()
        self.drift_sport = drift_sport
        self.fail = fail
        self.updates: list[dict] = []

    async def get(self, workout_id):
        return copy.deepcopy(self.state)

    async def update(self, workout_id, **kw):
        kw = {k: v for k, v in kw.items() if v is not None}
        self.updates.append(kw)
        if self.fail:
            return {"isError": True, "error_code": "API_ERROR", "message": self.fail}
        for k, v in kw.items():
            if k in self.stuck:
                continue
            if k == "duration_minutes":
                self.state["metrics"]["duration_planned"] = v / 60.0
            elif k == "tss_planned":
                self.state["metrics"]["tss_planned"] = v
            elif k == "distance_km":
                self.state["metrics"]["distance_planned_km"] = v
            elif k == "date":
                self.state["date"] = v + "T00:00:00"
            elif k == "structured_workout":
                self.state["structured_workout"] = copy.deepcopy(v)
            elif k == "sport":
                self.state["sport"] = v
            else:
                self.state[k] = v
        if self.drift_sport and "sport" not in kw:
            self.state["sport"] = self.drift_sport
        return {"success": True, "workout_id": str(workout_id), "message": "Workout updated successfully."}


def _wire(fake: FakeTP):
    return patch.multiple(
        lo_tools,
        tp_get_workout=AsyncMock(side_effect=fake.get),
        tp_update_workout=AsyncMock(side_effect=fake.update),
    )


# ---------------------------------------------------------------------------
# normalize_aliases
# ---------------------------------------------------------------------------


class TestNormalizeAliases:
    PROPS = {"workout_id": {}, "tss_planned": {}, "duration_minutes": {}}

    def test_alias_is_rewritten(self):
        out, notes = normalize_aliases(self.PROPS, {"workout_id": 1, "tss": 70})
        assert out == {"workout_id": 1, "tss_planned": 70}
        assert notes == ["tss -> tss_planned"]

    def test_alias_left_alone_when_canonical_also_given(self):
        out, notes = normalize_aliases(self.PROPS, {"tss": 70, "tss_planned": 72})
        assert out == {"tss": 70, "tss_planned": 72}
        assert notes == []

    def test_alias_left_alone_when_schema_lacks_canonical(self):
        out, _ = normalize_aliases({"workout_id": {}}, {"tss": 70})
        assert out == {"tss": 70}

    def test_alias_left_alone_when_schema_has_the_alias_itself(self):
        out, _ = normalize_aliases({"duration": {}, "duration_minutes": {}}, {"duration": 5})
        assert out == {"duration": 5}


# ---------------------------------------------------------------------------
# lo_update_workout_verified
# ---------------------------------------------------------------------------


class TestUpdateVerified:
    @pytest.mark.asyncio
    async def test_all_fields_land(self):
        fake = FakeTP(_detail())
        with _wire(fake):
            r = await lo_update_workout_verified(
                "1001", title="騎車・有氧耐力 3 小時", description="課表\n－－\n說明",
                duration_minutes=180, tss_planned=150, distance_km=90,
            )
        assert r["success"] is True
        assert "isError" not in r
        assert len(fake.updates) == 1
        assert all(v["ok"] for v in r["verified"].values())
        assert r["verified"]["duration_minutes"]["landed"] == 180

    @pytest.mark.asyncio
    async def test_structured_workout_is_split_into_second_write(self):
        fake = FakeTP(_detail())
        with _wire(fake):
            r = await lo_update_workout_verified(
                "1001", title="T", tss_planned=100, duration_minutes=30, structured_workout=_SW,
            )
        assert r["success"] is True
        assert [s["step"] for s in r["steps"]] == ["fields", "structure"]
        assert "structured_workout" not in fake.updates[0]
        assert set(fake.updates[1]) == {"structured_workout"}
        assert r["verified"]["structured_workout"]["ok"] is True
        assert r["verified"]["structured_workout"]["landed"]["blocks"] == 2

    @pytest.mark.asyncio
    async def test_simplified_structure_is_verified_by_total_length(self):
        fake = FakeTP(_detail())
        simplified = {
            "primaryIntensityMetric": "percentOfFtp",
            "steps": [
                {"name": "WU", "duration_seconds": 600, "intensity_min": 50, "intensity_max": 60},
                {"type": "repetition", "reps": 2, "steps": [
                    {"name": "on", "duration_seconds": 900, "intensity_min": 84, "intensity_max": 90},
                    {"name": "off", "duration_seconds": 300, "intensity_min": 50, "intensity_max": 55},
                ]},
            ],
        }

        async def update_converting(workout_id, **kw):
            # upstream converts simplified -> native; emulate with matching end
            if "structure" in kw:
                fake.state["structured_workout"] = {
                    "structure": [{"begin": 0, "end": 600}, {"begin": 600, "end": 3000}],
                    "polyline": [[0, 0]], "primaryLengthMetric": "duration",
                    "primaryIntensityMetric": "percentOfFtp",
                }
                kw = {k: v for k, v in kw.items() if k != "structure"}
            return await fake.update(workout_id, **kw)

        with patch.multiple(
            lo_tools,
            tp_get_workout=AsyncMock(side_effect=fake.get),
            tp_update_workout=AsyncMock(side_effect=update_converting),
        ):
            r = await lo_update_workout_verified("1001", structure=simplified, tss_planned=60)
        assert r["success"] is True
        assert r["verified"]["structure"]["ok"] is True
        assert r["verified"]["structure"]["sent"] == {"total_seconds": 3000}
        assert r["verified"]["structure"]["landed"]["end"] == 3000
        assert r["unverifiable"] == []

    @pytest.mark.asyncio
    async def test_structured_workout_alone_is_one_write(self):
        fake = FakeTP(_detail())
        with _wire(fake):
            r = await lo_update_workout_verified("1001", structured_workout=_SW)
        assert r["success"] is True
        assert [s["step"] for s in r["steps"]] == ["structure"]

    @pytest.mark.asyncio
    async def test_success_true_but_not_landed_is_reported(self):
        """TECH-37 reproduction: upstream says success, read-back unchanged."""
        fake = FakeTP(_detail(), stuck={"title", "tss_planned"})
        with _wire(fake):
            r = await lo_update_workout_verified("1001", title="T", tss_planned=100, duration_minutes=30)
        assert r["success"] is False
        assert r["isError"] is True
        assert r["error_code"] == "WRITE_NOT_LANDED"
        assert r["mismatched"] == ["title", "tss_planned"]
        assert r["verified"]["title"] == {"sent": "T", "landed": "再试试", "ok": False}
        assert r["verified"]["duration_minutes"]["ok"] is True

    @pytest.mark.asyncio
    async def test_description_echo_is_compact_when_ok_and_truncated_when_not(self):
        fake = FakeTP(_detail())
        with _wire(fake):
            r = await lo_update_workout_verified("1001", description="x" * 1000)
        assert r["verified"]["description"] == {"sent_len": 1000, "landed_len": 1000, "ok": True}
        fake = FakeTP(_detail(description="old"), stuck={"description"})
        with _wire(fake):
            r = await lo_update_workout_verified("1001", description="y" * 1000)
        d = r["verified"]["description"]
        assert d["ok"] is False and len(d["sent"]) == 400 and d["landed"] == "old"

    @pytest.mark.asyncio
    async def test_structure_fingerprint_catches_class_and_cadence_edits(self):
        base = copy.deepcopy(_SW)
        base["structure"][0]["steps"][0]["intensityClass"] = "rest"
        fake = FakeTP(_detail(structured_workout=base), stuck={"structured_workout"})
        edited = copy.deepcopy(_SW)
        edited["structure"][0]["steps"][0]["intensityClass"] = "warmUp"
        with _wire(fake):
            r = await lo_update_workout_verified("1001", structured_workout=edited)
        assert r["success"] is False
        assert r["verified"]["structured_workout"]["sent"]["classes"] == "w?"
        assert r["verified"]["structured_workout"]["landed"]["classes"] == "r?"

    @pytest.mark.asyncio
    async def test_sport_drift_without_sending_sport_is_caught(self):
        """TECH-16: description-only update flipped Race -> Run."""
        fake = FakeTP(_detail(sport="Race"), drift_sport="Run")
        with _wire(fake):
            r = await lo_update_workout_verified("1001", description="賽事說明")
        assert r["success"] is False
        assert "sport" in r["mismatched"]
        assert "TECH-16" in r["verified"]["sport"]["note"]

    @pytest.mark.asyncio
    async def test_structure_without_tss_is_refused_before_any_write(self):
        fake = FakeTP(_detail())
        with _wire(fake):
            r = await lo_update_workout_verified("1001", structure={"steps": []})
        assert r["isError"] is True
        assert r["error_code"] == "VALIDATION_ERROR"
        assert fake.updates == []

    @pytest.mark.asyncio
    async def test_upstream_error_is_propagated(self):
        fake = FakeTP(_detail(), fail="API error: 400 - tssPlanned must be a number")
        with _wire(fake):
            r = await lo_update_workout_verified("1001", tss_planned=100)
        assert r["isError"] is True
        assert "tssPlanned must be a number" in r["message"]

    @pytest.mark.asyncio
    async def test_unknown_field_rejected(self):
        fake = FakeTP(_detail())
        with _wire(fake):
            r = await lo_update_workout_verified("1001", tss=70)
        assert r["error_code"] == "INVALID_ARGS"
        assert fake.updates == []

    @pytest.mark.asyncio
    async def test_unverifiable_fields_do_not_fail(self):
        fake = FakeTP(_detail())
        with _wire(fake):
            r = await lo_update_workout_verified("1001", tags="kona", is_hidden=False, title="T")
        assert r["success"] is True
        assert r["unverifiable"] == ["is_hidden", "tags"]
        assert r["verified"]["tags"]["ok"] is None

    @pytest.mark.asyncio
    async def test_run_tss_mismatch_gets_tech26_warning(self):
        fake = FakeTP(_detail(sport="Run"), stuck={"tss_planned"})
        with _wire(fake):
            r = await lo_update_workout_verified("1001", tss_planned=60)
        assert r["success"] is False
        assert any("TECH-26" in w for w in r["warnings"])


# ---------------------------------------------------------------------------
# lo_set_sport
# ---------------------------------------------------------------------------


class TestSetSport:
    @pytest.mark.asyncio
    async def test_dayoff_to_swim_in_place(self):
        fake = FakeTP(_detail(sport="DayOff", title="休息日", workout_type=7))
        with _wire(fake):
            r = await lo_set_sport("1001", "Swim", title="游泳・有氧耐力 3000", tss_planned=45, duration_minutes=60)
        assert r["success"] is True
        assert r["sport_before"] == "DayOff"
        assert r["sport"] == "Swim"
        assert fake.updates[0]["sport"] == "Swim"
        assert fake.updates[0]["title"] == "游泳・有氧耐力 3000"

    @pytest.mark.asyncio
    async def test_title_is_resent_when_not_given(self):
        """TECH-25: sport alone does not take effect, so the current title rides along."""
        fake = FakeTP(_detail(sport="Bike", title="騎車"))
        with _wire(fake):
            r = await lo_set_sport("1001", "Run")
        assert r["success"] is True
        assert fake.updates[0] == {"title": "騎車", "sport": "Run"}

    @pytest.mark.asyncio
    async def test_race_is_refused(self):
        fake = FakeTP(_detail())
        with _wire(fake):
            r = await lo_set_sport("1001", "Race")
        assert r["error_code"] == "INVALID_ARGS"
        assert fake.updates == []

    @pytest.mark.asyncio
    async def test_sport_not_landing_is_reported(self):
        fake = FakeTP(_detail(sport="DayOff"), stuck={"sport"})
        with _wire(fake):
            r = await lo_set_sport("1001", "Swim")
        assert r["success"] is False
        assert r["mismatched"] == ["sport"]


# ---------------------------------------------------------------------------
# Registration in server.py
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tools_registered_with_metadata(self):
        from tp_mcp.server import _TOOL_HANDLERS, _TOOLS_BY_NAME

        for name in ("lo_update_workout_verified", "lo_set_sport"):
            tool = _TOOLS_BY_NAME[name]
            assert name in _TOOL_HANDLERS
            assert tool.title and not tool.title.startswith("Lo ")
            assert tool.annotations.read_only_hint is False
            assert tool.annotations.destructive_hint is False

    def test_verified_schema_tracks_upstream_update_schema(self):
        from tp_mcp.server import _TOOLS_BY_NAME

        up = set(_TOOLS_BY_NAME["tp_update_workout"].input_schema["properties"])
        lo = set(_TOOLS_BY_NAME["lo_update_workout_verified"].input_schema["properties"])
        assert lo == up | {"payload_file"}

    @pytest.mark.asyncio
    async def test_payload_file_merged_under_explicit_args(self, tmp_path):
        import json

        from tp_mcp.server import call_tool

        f = tmp_path / "fix.json"
        f.write_text(json.dumps({"workout_id": "1001", "title": "from file", "tss_planned": 70}), encoding="utf-8")
        fake = FakeTP(_detail())
        with _wire(fake):
            out = await call_tool("lo_update_workout_verified", {"payload_file": str(f), "tss_planned": 75})
        payload = json.loads(out[0].text)
        assert payload["success"] is True
        assert fake.updates == [{"title": "from file", "tss_planned": 75}]

    @pytest.mark.asyncio
    async def test_missing_workout_id_is_invalid(self):
        import json

        from tp_mcp.server import call_tool

        out = await call_tool("lo_update_workout_verified", {"title": "x"})
        assert json.loads(out[0].text)["error_code"] == "INVALID_ARGS"

    @pytest.mark.asyncio
    async def test_dispatch_through_call_tool(self):
        import json

        from tp_mcp.server import call_tool

        fake = FakeTP(_detail())
        with _wire(fake):
            out = await call_tool("lo_update_workout_verified", {"workout_id": "1001", "tss": 88})
        payload = json.loads(out[0].text)
        assert payload["success"] is True
        assert fake.updates == [{"tss_planned": 88}]
