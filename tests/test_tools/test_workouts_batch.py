"""Tests for tp_create_workouts_batch — LOCAL PATCH (羅教練 2026/08/21).

重點在三個「不該發生的事」：
  - payload 有錯時**一堂都不建**
  - 身分不符時**一堂都不建**
  - 逾時**不重試**（重試＝製造重複課）
"""

from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.tools.workouts_batch import tp_create_workouts_batch

PROFILE = {"athlete_id": 123456, "name": "Test Athlete"}


def _wo(day: str, sport: str = "Bike", title: str = "閾值間歇", **kw):
    payload = {"date": day, "sport": sport, "title": title, "duration_minutes": 60}
    payload.update(kw)
    return payload


def _detail(workout_id: str, day: str, title: str, sport: str = "Bike",
            description: str = "", structure=None, polyline=None,
            length_metric: str = "duration"):
    return {
        "id": workout_id,
        "date": day,
        "title": title,
        "sport": sport,
        "description": description,
        "completed": False,
        "metrics": {"duration_planned": 60, "tss_planned": 75.0,
                    "duration_actual": None, "tss_actual": None,
                    "distance_planned_km": None},
        "structured_workout": {
            "structure": structure if structure is not None else [
                {"begin": 0, "end": 3600, "steps": []},
            ],
            "polyline": polyline if polyline is not None else [[0, 0], [1, 1], [1, 0]],
            "primaryLengthMetric": length_metric,
        },
    }


def _patches(create=None, profile=None, get_one=None, get_many=None):
    """Patch the four collaborators the batch tool calls."""
    return (
        patch("tp_mcp.tools.workouts_batch.tp_get_profile",
              AsyncMock(return_value=profile if profile is not None else PROFILE)),
        patch("tp_mcp.tools.workouts_batch.tp_create_workout",
              create or AsyncMock(return_value={"success": True, "workout_id": 1})),
        patch("tp_mcp.tools.workouts_batch.tp_get_workout",
              get_one or AsyncMock(return_value=_detail("1", "2026-08-24", "閾值間歇"))),
        patch("tp_mcp.tools.workouts_batch.tp_get_workouts",
              get_many or AsyncMock(return_value={"workouts": []})),
    )


class TestNothingIsWrittenOnBadInput:
    """Preflight failures must not create anything."""

    @pytest.mark.asyncio
    async def test_invalid_sport_blocks_whole_batch(self):
        create = AsyncMock(return_value={"success": True, "workout_id": 1})
        p1, p2, p3, p4 = _patches(create=create)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(workouts=[
                _wo("2026-08-24"),
                _wo("2026-08-25", sport="Telepathy"),
            ])

        assert result["isError"] is True
        assert result["error_code"] == "PREFLIGHT_FAILED"
        assert result["created"] == 0
        assert any(e["index"] == 1 for e in result["errors"])
        create.assert_not_called()  # ← 第一筆合法，但仍然一堂都沒建

    @pytest.mark.asyncio
    async def test_duplicate_rows_within_batch_are_rejected(self):
        create = AsyncMock()
        p1, p2, p3, p4 = _patches(create=create)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(workouts=[
                _wo("2026-08-24"),
                _wo("2026-08-24"),
            ])

        assert result["error_code"] == "PREFLIGHT_FAILED"
        assert "重複" in result["errors"][0]["message"]
        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_size_cap(self):
        result = await tp_create_workouts_batch(
            workouts=[_wo("2026-08-24", title=f"課{i}") for i in range(31)],
        )
        assert result["error_code"] == "VALIDATION_ERROR"


class TestIdentityGuard:
    @pytest.mark.asyncio
    async def test_name_mismatch_rejects_entire_batch(self):
        create = AsyncMock()
        p1, p2, p3, p4 = _patches(create=create)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(
                workouts=[_wo("2026-08-24")],
                expect_athlete_name="Someone Else",
            )

        assert result["error_code"] == "ATHLETE_MISMATCH"
        assert result["athlete_name"] == "Test Athlete"
        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_name_proceeds(self):
        p1, p2, p3, p4 = _patches()
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(
                workouts=[_wo("2026-08-24")],
                expect_athlete_name="Test Athlete",
            )
        assert result["summary"]["created"] == 1

    @pytest.mark.asyncio
    async def test_profile_error_blocks_batch(self):
        create = AsyncMock()
        p1, p2, p3, p4 = _patches(
            create=create, profile={"isError": True, "message": "nope"},
        )
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(workouts=[_wo("2026-08-24")])

        assert result["error_code"] == "IDENTITY_CHECK_FAILED"
        create.assert_not_called()


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_creates_nothing(self):
        create = AsyncMock()
        p1, p2, p3, p4 = _patches(create=create)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(
                workouts=[_wo("2026-08-24"), _wo("2026-08-26", title="長跑", sport="Run")],
                dry_run=True,
            )

        assert result["dry_run"] is True
        assert result["would_create"] == 2
        assert result["date_range"] == {"start": "2026-08-24", "end": "2026-08-26"}
        create.assert_not_called()


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_existing_workout_is_skipped(self):
        create = AsyncMock(return_value={"success": True, "workout_id": 9})
        existing = AsyncMock(return_value={"workouts": [
            {"date": "2026-08-24", "sport": "Bike", "title": "閾值間歇"},
        ]})
        p1, p2, p3, p4 = _patches(create=create, get_many=existing)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(workouts=[
                _wo("2026-08-24"),
                _wo("2026-08-25", title="有氧耐力"),
            ])

        assert result["summary"]["skipped"] == 1
        assert result["summary"]["created"] == 1
        assert create.call_count == 1

    @pytest.mark.asyncio
    async def test_preexisting_other_workout_on_a_target_day_is_reported(self):
        """skip_if_exists only matches day+sport+title. A workout the coach
        already put on that day under a different title slips through, and the
        day quietly ends up with two overlapping sessions (2026/09/21: Mark
        Huang had two 移動日 on 10/1). Report it; deciding is the coach's."""
        existing = AsyncMock(return_value={"workouts": [
            {"id": "77", "date": "2026-08-24", "sport": "Other",
             "title": "移動日", "tss_planned": None},
        ]})
        p1, p2, p3, p4 = _patches(get_many=existing)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(workouts=[_wo("2026-08-24")])

        assert result["summary"]["created"] == 1          # not blocked
        assert [w["id"] for w in result["preexisting_on_target_days"]] == ["77"]
        assert "先問再做" in result["preexisting_note"]

    @pytest.mark.asyncio
    async def test_preexisting_excludes_rows_this_batch_already_skips(self):
        """A same-key row is already reported as `skipped`; listing it again
        under preexisting would double-count it."""
        existing = AsyncMock(return_value={"workouts": [
            {"id": "5", "date": "2026-08-24", "sport": "Bike", "title": "閾值間歇"},
        ]})
        p1, p2, p3, p4 = _patches(get_many=existing)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(workouts=[_wo("2026-08-24")])

        assert result["summary"]["skipped"] == 1
        assert "preexisting_on_target_days" not in result

    @pytest.mark.asyncio
    async def test_preexisting_ignores_days_outside_this_batch(self):
        existing = AsyncMock(return_value={"workouts": [
            {"id": "88", "date": "2026-08-26", "sport": "Run", "title": "跑步團練"},
        ]})
        p1, p2, p3, p4 = _patches(get_many=existing)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(workouts=[_wo("2026-08-24")])

        assert "preexisting_on_target_days" not in result

    @pytest.mark.asyncio
    async def test_whole_week_readback_replaces_created_rows_only(self, tmp_path):
        """Week-level rules (R6/R14/R45) read a created-rows-only file as false
        FAILs, and the week load then needs a second call to compute."""
        week = AsyncMock(return_value={
            "success": True, "count": 11,
            "rows": [{"sport": "Bike", "tss_planned": 60.9},
                     {"sport": "Run", "tss_planned": 65.0}],
        })
        p1, p2, p3, p4 = _patches()
        with p1, p2, p3, p4, patch(
            "tp_mcp.tools.lo_tools.lo_get_week_for_validate", week
        ):
            result = await tp_create_workouts_batch(
                workouts=[_wo("2026-08-24")],
                readback_save_to=str(tmp_path / "week.json"),
                readback_week_start="2026-08-24",
                readback_week_end="2026-08-30",
                target_tss="100-150",
            )

        assert result["readback_scope"] == "week"
        assert result["readback_count"] == 11
        assert result["week_load"]["tri_tss"] == 125.9
        assert result["week_load"]["in_range"] is True
        assert "readback" not in result          # payload stayed out of context

    @pytest.mark.asyncio
    async def test_created_rows_only_readback_says_so(self, tmp_path):
        p1, p2, p3, p4 = _patches()
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(
                workouts=[_wo("2026-08-24")],
                readback_save_to=str(tmp_path / "rows.json"),
            )
        assert result["readback_scope"] == "created_rows_only"
        assert "readback_week_start/end" in result["readback_note"]

    @pytest.mark.asyncio
    async def test_whole_week_readback_failure_falls_back(self, tmp_path):
        week = AsyncMock(return_value={"isError": True, "message": "boom"})
        p1, p2, p3, p4 = _patches()
        with p1, p2, p3, p4, patch(
            "tp_mcp.tools.lo_tools.lo_get_week_for_validate", week
        ):
            result = await tp_create_workouts_batch(
                workouts=[_wo("2026-08-24")],
                readback_save_to=str(tmp_path / "week.json"),
                readback_week_start="2026-08-24",
                readback_week_end="2026-08-30",
            )
        assert "whole-week readback failed" in result["warnings"][0]
        assert result["readback"]["count"] == 1   # the created row is still returned

    @pytest.mark.asyncio
    async def test_precheck_failure_blocks_batch(self):
        create = AsyncMock()
        broken = AsyncMock(return_value={"isError": True, "message": "boom"})
        p1, p2, p3, p4 = _patches(create=create, get_many=broken)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(workouts=[_wo("2026-08-24")])

        assert result["error_code"] == "PRECHECK_FAILED"
        create.assert_not_called()


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_stop_on_error_leaves_later_rows_untouched(self):
        create = AsyncMock(side_effect=[
            {"success": True, "workout_id": 1},
            {"isError": True, "error_code": "API_ERROR", "message": "bad"},
            {"success": True, "workout_id": 3},
        ])
        p1, p2, p3, p4 = _patches(create=create)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(workouts=[
                _wo("2026-08-24"),
                _wo("2026-08-25", title="有氧耐力"),
                _wo("2026-08-26", title="長跑", sport="Run"),
            ])

        assert create.call_count == 2  # 第三筆沒送出
        assert result["summary"]["created"] == 1
        assert result["summary"]["failed"] == 1
        assert result["stopped_at_index"] == 1
        assert result["summary"]["not_attempted"] == 1

    @pytest.mark.asyncio
    async def test_continue_mode_keeps_going(self):
        create = AsyncMock(side_effect=[
            {"isError": True, "error_code": "API_ERROR", "message": "bad"},
            {"success": True, "workout_id": 2},
        ])
        p1, p2, p3, p4 = _patches(create=create)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(
                workouts=[_wo("2026-08-24"), _wo("2026-08-25", title="有氧耐力")],
                on_error="continue",
            )

        assert create.call_count == 2
        assert result["summary"]["created"] == 1
        assert result["summary"]["failed"] == 1

    @pytest.mark.asyncio
    async def test_timeout_is_uncertain_and_never_retried(self):
        create = AsyncMock(side_effect=TimeoutError("read timeout"))
        p1, p2, p3, p4 = _patches(create=create)
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(workouts=[
                _wo("2026-08-24"),
                _wo("2026-08-25", title="有氧耐力"),
            ])

        assert create.call_count == 1  # ← 關鍵：沒有第二次呼叫
        assert result["summary"]["uncertain"] == 1
        assert result["results"][0]["status"] == "uncertain"
        assert result["success"] is False


class TestReadback:
    @pytest.mark.asyncio
    async def test_readback_is_flattened_for_validate_week(self):
        """validate_week.py 讀頂層 duration_planned／tss_planned／structured_workout。"""
        detail = _detail("77", "2026-08-24", "閾值間歇", description="課表\n－－\n說明")
        p1, p2, p3, p4 = _patches(
            create=AsyncMock(return_value={"success": True, "workout_id": 77}),
            get_one=AsyncMock(return_value=detail),
        )
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(
                workouts=[_wo("2026-08-24", description="課表\n－－\n說明")],
            )

        row = result["readback"]["workouts"][0]
        assert row["tss_planned"] == 75.0        # 從 metrics 攤平到頂層
        assert row["duration_planned"] == 60
        assert row["structured_workout"]["structure"]
        assert row["type"] == "planned"
        assert result["results"][0]["echo"]["description_match"] is True

    @pytest.mark.asyncio
    async def test_description_mismatch_is_warned_not_resent(self):
        detail = _detail("77", "2026-08-24", "閾值間歇", description="被截斷的說明")
        create = AsyncMock(return_value={"success": True, "workout_id": 77})
        p1, p2, p3, p4 = _patches(create=create, get_one=AsyncMock(return_value=detail))
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(
                workouts=[_wo("2026-08-24", description="完整的說明")],
            )

        row = result["results"][0]
        assert row["echo"]["description_match"] is False
        assert any("不符" in w for w in row["warnings"])
        assert create.call_count == 1  # 不自動重送

    @pytest.mark.asyncio
    async def test_distance_run_seconds_in_begin_end_is_flagged(self):
        """TECH-05：距離型跑課的 begin/end 必須是累計公尺，秒值要被抓出來。"""
        detail = _detail(
            "88", "2026-08-26", "長跑", sport="Run",
            structure=[{"begin": 0, "end": 300, "steps": []}],
            length_metric="distance",
        )
        p1, p2, p3, p4 = _patches(
            create=AsyncMock(return_value={"success": True, "workout_id": 88}),
            get_one=AsyncMock(return_value=detail),
        )
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(
                workouts=[_wo("2026-08-26", sport="Run", title="長跑")],
            )

        assert any("累計公尺" in w for w in result["results"][0]["warnings"])

    @pytest.mark.asyncio
    async def test_readback_save_to_keeps_payload_out_of_context(self, tmp_path):
        target = tmp_path / "week.json"
        p1, p2, p3, p4 = _patches(
            create=AsyncMock(return_value={"success": True, "workout_id": 1}),
        )
        with p1, p2, p3, p4:
            result = await tp_create_workouts_batch(
                workouts=[_wo("2026-08-24")],
                readback_save_to=str(target),
            )

        assert "readback" not in result
        assert result["readback_json_path"] == str(target)
        assert target.exists()

        import json
        saved = json.loads(target.read_text(encoding="utf-8"))
        assert saved["workouts"][0]["tss_planned"] == 75.0


class TestDayOff:
    @pytest.mark.asyncio
    async def test_dayoff_is_created_at_one_minute_then_zeroed(self):
        create = AsyncMock(return_value={"success": True, "workout_id": 5})
        update = AsyncMock(return_value={"success": True})
        p1, p2, p3, p4 = _patches(
            create=create,
            get_one=AsyncMock(return_value=_detail("5", "2026-08-24", "休息日",
                                                   sport="DayOff")),
        )
        with p1, p2, p3, p4, patch(
            "tp_mcp.tools.workouts_batch.tp_update_workout", update,
        ):
            result = await tp_create_workouts_batch(workouts=[{
                "date": "2026-08-24", "sport": "DayOff", "title": "休息日",
                "description": "三條式",
            }])

        assert create.call_args.kwargs["duration_minutes"] == 1
        assert update.call_args.kwargs["duration_minutes"] == 0
        assert result["results"][0]["dayoff_zeroed"] is True


# ---------------------------------------------------------------------------
# End-to-end через server dispatch: athlete targeting + handler wiring
# ---------------------------------------------------------------------------


class TestBatchDispatch:
    """LOCAL PATCH: batch create through call_tool (athlete pop + handler)."""

    @pytest.mark.asyncio
    async def test_athlete_arg_is_consumed_and_batch_runs(self):
        import json

        from tp_mcp.server import call_tool

        p1, p2, p3, p4 = _patches(
            create=AsyncMock(return_value={"success": True, "workout_id": 42}),
            get_one=AsyncMock(return_value=_detail("42", "2026-08-24", "閾值間歇")),
        )
        with p1, p2, p3, p4:
            raw = await call_tool("tp_create_workouts_batch", {
                "athlete": "Test Athlete",
                "workouts": [_wo("2026-08-24")],
                "expect_athlete_name": "Test Athlete",
            })

        result = json.loads(raw[0].text)
        assert result["summary"]["created"] == 1
        assert result["athlete_name"] == "Test Athlete"

    @pytest.mark.asyncio
    async def test_missing_workouts_arg_is_a_clean_error(self):
        import json

        from tp_mcp.server import call_tool

        raw = await call_tool("tp_create_workouts_batch", {"athlete": "Test Athlete"})
        result = json.loads(raw[0].text)
        assert result["isError"] is True
        assert result["error_code"] == "INVALID_ARGS"
