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
        t = r["verified"]["title"]
        assert t["ok"] is False and t["len"] == 1 and t["landed_len"] == 3 and t["diff"]
        assert r["verified"]["duration_minutes"]["ok"] is True

    @pytest.mark.asyncio
    async def test_description_echo_is_compact_when_ok_and_truncated_when_not(self):
        fake = FakeTP(_detail())
        with _wire(fake):
            r = await lo_update_workout_verified("1001", description="x" * 1000)
        d = r["verified"]["description"]
        assert d["ok"] is True and d["len"] == 1000 and len(d["sha8"]) == 8 and "sent" not in d
        fake = FakeTP(_detail(description="line1\nold\nline3"), stuck={"description"})
        with _wire(fake):
            r = await lo_update_workout_verified("1001", description="line1\nnew\nline3")
        d = r["verified"]["description"]
        assert d["ok"] is False and "sent" not in d
        assert any(ln.startswith("-old") for ln in d["diff"]) and any(ln.startswith("+new") for ln in d["diff"])
        with _wire(fake):
            r = await lo_update_workout_verified("1001", description="line1\nnew\nline3", verbose=True)
        assert r["verified"]["description"]["sent"] == "line1\nnew\nline3"

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
        assert lo == up | {"payload_file", "verbose"}

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


# ---------------------------------------------------------------------------
# lo_update_workouts_batch
# ---------------------------------------------------------------------------


class MultiFake:
    """Several workouts behind one fake TP (id -> FakeTP)."""

    def __init__(self, fakes):
        self.fakes = fakes

    async def get(self, workout_id):
        return await self.fakes[str(workout_id)].get(workout_id)

    async def update(self, workout_id, **kw):
        return await self.fakes[str(workout_id)].update(workout_id, **kw)


def _wire_multi(multi):
    return patch.multiple(
        lo_tools,
        tp_get_workout=AsyncMock(side_effect=multi.get),
        tp_update_workout=AsyncMock(side_effect=multi.update),
    )


class TestUpdateBatch:
    @pytest.mark.asyncio
    async def test_all_rows_verified_and_readback_written(self, tmp_path):
        import json

        from tp_mcp.tools.lo_tools import lo_update_workouts_batch

        multi = MultiFake({"1": FakeTP(_detail(id="1")), "2": FakeTP(_detail(id="2", sport="Run"))})
        rb = tmp_path / "rb.json"
        with _wire_multi(multi):
            r = await lo_update_workouts_batch(
                [{"workout_id": "1", "title": "A", "tss_planned": 50},
                 {"workout_id": "2", "description": "d", "structured_workout": _SW}],
                readback_save_to=str(rb),
            )
        assert r["success"] is True
        assert r["summary"] == {"total": 2, "verified": 2, "failed": 0, "not_attempted": 0}
        assert [row["steps"] for row in r["results"]] == [["fields"], ["fields", "structure"]]
        assert "verified" not in r["results"][0]  # compact rows when ok
        data = json.loads(rb.read_text(encoding="utf-8"))
        assert [w["id"] for w in data["workouts"]] == ["1", "2"]
        assert data["workouts"][1]["structured_workout"]["structure"]

    @pytest.mark.asyncio
    async def test_stop_on_first_failure(self):
        from tp_mcp.tools.lo_tools import lo_update_workouts_batch

        multi = MultiFake({"1": FakeTP(_detail(id="1"), stuck={"title"}), "2": FakeTP(_detail(id="2"))})
        with _wire_multi(multi):
            r = await lo_update_workouts_batch([{"workout_id": "1", "title": "A"}, {"workout_id": "2", "title": "B"}])
        assert r["success"] is False and r["error_code"] == "WRITE_NOT_LANDED"
        assert r["summary"] == {"total": 2, "verified": 0, "failed": 1, "not_attempted": 1}
        assert r["results"][0]["mismatched"] == ["title"]
        assert r["results"][0]["verified"]["title"]["ok"] is False
        assert r["results"][1]["status"] == "not_attempted"
        assert multi.fakes["2"].updates == []

    @pytest.mark.asyncio
    async def test_continue_mode(self):
        from tp_mcp.tools.lo_tools import lo_update_workouts_batch

        multi = MultiFake({"1": FakeTP(_detail(id="1"), stuck={"title"}), "2": FakeTP(_detail(id="2"))})
        with _wire_multi(multi):
            r = await lo_update_workouts_batch(
                [{"workout_id": "1", "title": "A"}, {"workout_id": "2", "title": "B"}], on_error="continue"
            )
        assert r["summary"] == {"total": 2, "verified": 1, "failed": 1, "not_attempted": 0}

    @pytest.mark.asyncio
    async def test_payload_file_dispatch_and_athlete(self, tmp_path):
        import json

        from tp_mcp.client.context import athlete_override
        from tp_mcp.server import call_tool

        f = tmp_path / "upd.json"
        f.write_text(json.dumps({"athlete": "777", "updates": [{"workout_id": "1", "title": "A"}]}), encoding="utf-8")
        seen = {}
        fake = FakeTP(_detail(id="1"))

        async def get_spy(workout_id):
            seen["athlete"] = athlete_override.get()
            return await fake.get(workout_id)

        with patch.multiple(lo_tools, tp_get_workout=AsyncMock(side_effect=get_spy),
                            tp_update_workout=AsyncMock(side_effect=fake.update)):
            out = await call_tool("lo_update_workouts_batch", {"payload_file": str(f)})
        payload = json.loads(out[0].text)
        assert payload["success"] is True
        assert seen["athlete"] == "777"

    @pytest.mark.asyncio
    async def test_row_without_workout_id_rejected(self):
        from tp_mcp.tools.lo_tools import lo_update_workouts_batch

        r = await lo_update_workouts_batch([{"title": "x"}])
        assert r["error_code"] == "INVALID_ARGS"


# ---------------------------------------------------------------------------
# lo_get_week_for_validate
# ---------------------------------------------------------------------------


class TestGetWeekForValidate:
    @pytest.mark.asyncio
    async def test_structure_sports_get_detail_and_file_is_validate_shaped(self, tmp_path):
        import json

        from tp_mcp.tools import workouts as wmod
        from tp_mcp.tools.lo_tools import lo_get_week_for_validate

        listed = {"workouts": [
            {"id": "1", "date": "2026-09-21", "sport": "Bike", "title": "有氧耐力",
             "duration_planned": 1.0, "tss_planned": 45},
            {"id": "2", "date": "2026-09-22", "sport": "Swim", "title": "閾值間歇",
             "duration_planned": 1.0, "tss_planned": 48},
            {"id": "3", "date": "2026-09-23", "sport": "Run", "title": "節奏跑",
             "duration_planned": 1.0, "tss_planned": 60},
        ]}
        details = {
            "1": _detail(id="1", sport="Bike", title="有氧耐力", structured_workout=_SW),
            "3": _detail(id="3", sport="Run", title="節奏跑", structured_workout=_SW),
        }
        calls = []

        async def get_detail(workout_id):
            calls.append(workout_id)
            return details[str(workout_id)]

        out = tmp_path / "week.json"
        with patch.object(wmod, "tp_get_workouts", AsyncMock(return_value=listed)), \
             patch.object(lo_tools, "tp_get_workout", AsyncMock(side_effect=get_detail)):
            r = await lo_get_week_for_validate("2026-09-21", "2026-09-27", str(out))
        assert r["success"] is True
        assert calls == ["1", "3"]  # swim never needs a detail call
        assert r["count"] == 3 and r["with_structure"] == 2 and r["detail_calls"] == 2
        data = json.loads(out.read_text(encoding="utf-8"))
        ws = data["workouts"]
        assert [w["id"] for w in ws] == ["1", "2", "3"]
        assert ws[0]["structured_workout"]["structure"] and "duration_planned" in ws[0] and "tss_planned" in ws[0]
        assert ws[1]["sport"] == "Swim" and "structured_workout" not in ws[1]
        assert ws[1]["type"] == "planned"

    @pytest.mark.asyncio
    async def test_detail_failure_falls_back_to_list_row(self, tmp_path):
        from tp_mcp.tools import workouts as wmod
        from tp_mcp.tools.lo_tools import lo_get_week_for_validate

        listed = {"workouts": [{"id": "1", "date": "2026-09-21", "sport": "Bike", "title": "T"}]}
        with patch.object(wmod, "tp_get_workouts", AsyncMock(return_value=listed)), \
             patch.object(lo_tools, "tp_get_workout", AsyncMock(return_value={"isError": True, "message": "x"})):
            r = await lo_get_week_for_validate("2026-09-21", "2026-09-27", str(tmp_path / "w.json"))
        assert r["detail_failures"] == ["1"] and r["count"] == 1 and r["with_structure"] == 0

    def test_registered_read_only(self):
        from tp_mcp.server import _TOOL_HANDLERS, _TOOLS_BY_NAME

        assert "lo_get_week_for_validate" in _TOOL_HANDLERS
        assert _TOOLS_BY_NAME["lo_get_week_for_validate"].annotations.read_only_hint is True
        assert _TOOLS_BY_NAME["lo_update_workouts_batch"].annotations.read_only_hint is False

    @pytest.mark.asyncio
    async def test_save_to_reaches_handler_through_dispatch(self, tmp_path):
        """Bug 2026-09-16 (2nd session): dispatch popped save_to for every tool, so this
        tool's own required `save_to` was reported missing."""
        import json

        from tp_mcp.server import call_tool
        from tp_mcp.tools import workouts as wmod

        listed = {"workouts": [{"id": "1", "date": "2026-09-21", "sport": "Swim", "title": "T"}]}
        out = tmp_path / "w.json"
        with patch.object(wmod, "tp_get_workouts", AsyncMock(return_value=listed)):
            res = await call_tool(
                "lo_get_week_for_validate",
                {"athlete": "1", "start_date": "2026-09-21", "end_date": "2026-09-27", "save_to": str(out)},
            )
        payload = json.loads(res[0].text)
        assert payload.get("success") is True, payload
        assert payload["saved_to"] == str(out) and out.exists()

    @pytest.mark.asyncio
    async def test_generic_save_to_dump_still_works_for_other_tools(self, tmp_path):
        import json

        from tp_mcp.server import _TOOL_HANDLERS, call_tool

        original = _TOOL_HANDLERS["tp_get_workout"]
        _TOOL_HANDLERS["tp_get_workout"] = AsyncMock(return_value={"id": "1", "title": "x", "metrics": {}})
        try:
            out = tmp_path / "d.json"
            res = await call_tool("tp_get_workout", {"workout_id": "1", "save_to": str(out)})
        finally:
            _TOOL_HANDLERS["tp_get_workout"] = original
        payload = json.loads(res[0].text)
        assert payload.get("saved_to") == str(out) and out.exists()


class TestUpdateBatchWeekReadback:
    @pytest.mark.asyncio
    async def test_whole_week_readback_when_range_given(self, tmp_path):
        import json

        from tp_mcp.tools import workouts as wmod
        from tp_mcp.tools.lo_tools import lo_update_workouts_batch

        fake = FakeTP(_detail(id="1", sport="Swim"))
        listed = {"workouts": [
            {"id": "1", "date": "2026-09-21", "sport": "Swim", "title": "T"},
            {"id": "9", "date": "2026-09-23", "sport": "Swim", "title": "團練"},
        ]}
        rb = tmp_path / "week.json"
        with _wire(fake), patch.object(wmod, "tp_get_workouts", AsyncMock(return_value=listed)):
            r = await lo_update_workouts_batch(
                [{"workout_id": "1", "title": "A"}], readback_save_to=str(rb),
                readback_week_start="2026-09-21", readback_week_end="2026-09-27",
            )
        assert r["success"] is True and r["readback_scope"] == "week" and r["readback_count"] == 2
        assert [w["id"] for w in json.loads(rb.read_text(encoding="utf-8"))["workouts"]] == ["1", "9"]

    @pytest.mark.asyncio
    async def test_rows_only_readback_is_labelled(self, tmp_path):
        from tp_mcp.tools.lo_tools import lo_update_workouts_batch

        fake = FakeTP(_detail(id="1"))
        with _wire(fake):
            r = await lo_update_workouts_batch(
                [{"workout_id": "1", "title": "A"}], readback_save_to=str(tmp_path / "r.json")
            )
        assert r["readback_scope"] == "updated_rows_only" and "R14" in r["readback_note"]
