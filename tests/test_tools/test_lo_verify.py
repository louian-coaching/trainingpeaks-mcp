"""Tests for tools/lo_verify.py (羅教練 fork-only per-segment verification)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.tools.lo_verify import flatten_structure, lo_verify_intervals

# ---------------------------------------------------------------------------
# Fixtures: a 2 x 20min race-power ride, the exact shape that motivated the tool
# ---------------------------------------------------------------------------

_STRUCTURE = [
    {
        "begin": 0, "end": 600,
        "length": {"value": 1, "unit": "repetition"},
        "steps": [{
            "name": "熱身騎", "length": {"value": 600, "unit": "second"},
            "targets": [{"minValue": 50, "maxValue": 65}], "intensityClass": "warmUp",
        }],
    },
    {
        "begin": 600, "end": 3600,
        "length": {"value": 2, "unit": "repetition"},
        "steps": [
            {"name": "比賽強度段", "length": {"value": 1200, "unit": "second"},
             "targets": [{"minValue": 75, "maxValue": 81}], "intensityClass": "active"},
            {"name": "恢復", "length": {"value": 300, "unit": "second"},
             "targets": [{"minValue": 50, "maxValue": 60}], "intensityClass": "rest"},
        ],
    },
]


def _series(total_s: int, power_at, step: int = 1):
    """One sample per `step` seconds; `power_at(t)` gives the watt value."""
    return [{"time": t, "Power": power_at(t), "HeartRate": 130, "Cadence": 85}
            for t in range(0, total_s, step)]


def _flat_ride():
    """First block holds 210W, second fades to 190W — a real PER-24 failure."""
    def p(t):
        if t < 600:
            return 140
        if 600 <= t < 1800:
            return 210
        if 1800 <= t < 2100:
            return 140
        if 2100 <= t < 3300:
            return 210 if t < 2700 else 190
        return 140
    return _series(3600, p)


def _patched(detail_sw=None, series=None, laps=None, tmp_path=None):
    """Patch tp_analyze_workout + tp_get_workout; analysis writes a real file."""
    data_file = tmp_path / "analysis.json"
    data_file.write_text(json.dumps({"data": series if series is not None else _flat_ride()}))
    analysis = {
        "workoutId": 1, "data_file": str(data_file),
        "lapData": laps if laps is not None else [{"Name": "Lap 1"}],
    }
    detail = {"id": "1", "structured_workout": detail_sw}
    return (
        patch("tp_mcp.tools.analyze.tp_analyze_workout", AsyncMock(return_value=analysis)),
        patch("tp_mcp.tools.workouts.tp_get_workout", AsyncMock(return_value=detail)),
    )


def _sw(structure=None, metric="duration"):
    return {"structure": structure if structure is not None else _STRUCTURE,
            "primaryLengthMetric": metric, "primaryIntensityMetric": "percentOfFtp"}


# ---------------------------------------------------------------------------
# flatten_structure
# ---------------------------------------------------------------------------


def test_flatten_expands_repetitions_end_to_end():
    segs = flatten_structure(_STRUCTURE)
    assert [s["name"] for s in segs] == [
        "熱身騎", "比賽強度段 #1", "恢復 #1", "比賽強度段 #2", "恢復 #2",
    ]
    assert [(s["start"], s["end"]) for s in segs] == [
        (0, 600), (600, 1800), (1800, 2100), (2100, 3300), (3300, 3600),
    ]


def test_flatten_reanchors_on_block_begin():
    """A block's own `begin` wins over our running sum, so one bad step
    cannot shift every later segment."""
    structure = [
        {"begin": 0, "end": 100, "steps": [
            {"name": "a", "length": {"value": 999, "unit": "second"}}]},
        {"begin": 100, "end": 400, "steps": [
            {"name": "b", "length": {"value": 300, "unit": "second"}}]},
    ]
    segs = flatten_structure(structure)
    assert (segs[1]["start"], segs[1]["end"]) == (100, 400)


def test_flatten_skips_zero_length_steps():
    structure = [{"begin": 0, "end": 60, "steps": [
        {"name": "ghost", "length": {"value": 0, "unit": "second"}},
        {"name": "real", "length": {"value": 60, "unit": "second"}},
    ]}]
    assert [s["name"] for s in flatten_structure(structure)] == ["real"]


def test_flatten_tolerates_junk():
    assert flatten_structure([]) == []
    assert flatten_structure([{"begin": 0, "steps": None}]) == []


# ---------------------------------------------------------------------------
# The core case: two work blocks, no usable laps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconstructs_work_segments_without_laps(tmp_path):
    a, w = _patched(detail_sw=_sw(), tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1", ftp=270)
    assert res["success"] is True
    assert res["segment_source"] == "structured_workout"
    # rest/warm-up hidden by default ⇒ only the two work blocks
    names = [r["name"] for r in res["segments"]]
    assert names == ["比賽強度段 #1", "比賽強度段 #2"]
    first, second = res["segments"]
    assert first["avg_power"] == 210.0
    assert second["avg_power"] == 200.0  # 600s @210 then 600s @190
    # the whole point: the fade inside block 2 is visible
    assert second["half_split_w"] == -20.0
    assert first["half_split_w"] == 0.0


@pytest.mark.asyncio
async def test_target_watts_and_in_range(tmp_path):
    a, w = _patched(detail_sw=_sw(), tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1", ftp=270)
    first = res["segments"][0]
    assert first["target_pct"] == [75, 81]
    assert first["target_w"] == [203, 219]   # 270W × 75~81%, .5 rounds up not to even
    assert first["in_range_pct"] == 100.0
    assert res["segments"][1]["in_range_pct"] == 50.0  # second half drops to 190W


@pytest.mark.asyncio
async def test_single_lap_note_present(tmp_path):
    a, w = _patched(detail_sw=_sw(), tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1")
    assert "single lap" in res["note"]


@pytest.mark.asyncio
async def test_include_rest_shows_every_segment(tmp_path):
    a, w = _patched(detail_sw=_sw(), tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1", include_rest=True)
    assert len(res["segments"]) == 5


# ---------------------------------------------------------------------------
# Alignment / drift — a drifted overlay must never pass silently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_warning_when_actual_runs_long(tmp_path):
    a, w = _patched(detail_sw=_sw(), series=_series(4000, lambda t: 200), tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1")
    assert res["drift_pct"] == pytest.approx(11.1, abs=0.2)
    assert "align='scale'" in res["warning"]


@pytest.mark.asyncio
async def test_small_drift_reports_but_does_not_warn(tmp_path):
    a, w = _patched(detail_sw=_sw(), series=_series(3660, lambda t: 200), tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1")
    assert res["drift_pct"] == pytest.approx(1.7, abs=0.2)
    assert "warning" not in res


@pytest.mark.asyncio
async def test_scale_stretches_boundaries(tmp_path):
    a, w = _patched(detail_sw=_sw(), series=_series(7200, lambda t: 200), tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1", align="scale")
    # every boundary doubled with the ~2x elapsed time
    assert res["segments"][0]["start_s"] == pytest.approx(1200, abs=1)
    assert res["segments"][0]["end_s"] == pytest.approx(3600, abs=1)
    assert "warning" not in res


@pytest.mark.asyncio
async def test_rejects_unknown_align(tmp_path):
    a, w = _patched(detail_sw=_sw(), tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1", align="stretch")
    assert res["error_code"] == "INVALID_ARGS"


# ---------------------------------------------------------------------------
# Distance-axis runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distance_axis_uses_distance_channel(tmp_path):
    structure = [{"begin": 0, "end": 5000, "length": {"value": 1, "unit": "repetition"},
                  "steps": [{"name": "比賽強度段", "length": {"value": 5000, "unit": "meter"},
                             "targets": [{"minValue": 80, "maxValue": 85}],
                             "intensityClass": "active"}]}]
    series = [{"time": t, "Distance": t / 1000.0, "Power": 260} for t in range(0, 5000, 10)]
    a, w = _patched(detail_sw=_sw(structure, metric="distance"), series=series, tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1", ftp=320)
    assert res["axis"] == "distance"
    seg = res["segments"][0]
    assert seg["start_m"] == 0.0 and seg["end_m"] == 5000.0
    assert seg["avg_power"] == 260.0
    assert seg["in_range_pct"] == 100.0   # 320W × 80~85% = 256~272W


# ---------------------------------------------------------------------------
# Manual segments + failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_segments_bypass_structure(tmp_path):
    a, w = _patched(detail_sw=None, tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals(
            "1", segments=[{"name": "last 10min", "start_s": 3000, "end_s": 3600}])
    assert res["segment_source"] == "manual"
    assert res["segments"][0]["name"] == "last 10min"
    assert "drift_pct" not in res  # nothing prescribed to compare against


@pytest.mark.asyncio
async def test_manual_segments_validated(tmp_path):
    a, w = _patched(tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1", segments=[{"start_s": 100, "end_s": 50}])
    assert res["error_code"] == "INVALID_ARGS"
    assert "greater than" in res["message"]


@pytest.mark.asyncio
async def test_no_structure_is_actionable(tmp_path):
    a, w = _patched(detail_sw=None, tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1")
    assert res["error_code"] == "NO_STRUCTURE"
    assert "segments" in res["message"]


@pytest.mark.asyncio
async def test_no_timeseries_is_actionable(tmp_path):
    a, w = _patched(detail_sw=_sw(), series=[], tmp_path=tmp_path)
    with a, w:
        res = await lo_verify_intervals("1")
    assert res["error_code"] == "NO_TIMESERIES"
    assert "manual entry" in res["message"]


@pytest.mark.asyncio
async def test_analysis_error_propagates(tmp_path):
    err = {"isError": True, "error_code": "NOT_FOUND", "message": "gone"}
    with patch("tp_mcp.tools.analyze.tp_analyze_workout", AsyncMock(return_value=err)):
        res = await lo_verify_intervals("1")
    assert res["error_code"] == "NOT_FOUND"
