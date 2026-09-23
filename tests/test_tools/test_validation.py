"""Tests for Pydantic input validation models."""

import pytest
from pydantic import ValidationError

from tp_mcp.tools._validation import (
    CreateWorkoutInput,
    DateRangeInput,
    FitnessInput,
    PeaksInput,
    UpdateWorkoutInput,
    WorkoutIdInput,
    format_validation_error,
)


class TestWorkoutIdInput:
    """Tests for WorkoutIdInput validation."""

    def test_valid_int(self):
        result = WorkoutIdInput(workout_id=123)
        assert result.workout_id == 123

    def test_valid_string(self):
        result = WorkoutIdInput(workout_id="456")
        assert result.workout_id == 456

    def test_invalid_string(self):
        with pytest.raises(ValidationError):
            WorkoutIdInput(workout_id="abc")

    def test_zero(self):
        with pytest.raises(ValidationError):
            WorkoutIdInput(workout_id=0)

    def test_negative(self):
        with pytest.raises(ValidationError):
            WorkoutIdInput(workout_id=-1)


class TestDateRangeInput:
    """Tests for DateRangeInput validation."""

    def test_valid(self):
        result = DateRangeInput(start_date="2025-01-01", end_date="2025-01-31")
        assert result.start_date.isoformat() == "2025-01-01"
        assert result.end_date.isoformat() == "2025-01-31"

    def test_inverted(self):
        with pytest.raises(ValidationError, match="start_date must be before"):
            DateRangeInput(start_date="2025-02-01", end_date="2025-01-01")

    def test_over_90_days(self):
        with pytest.raises(ValidationError, match="90 days"):
            DateRangeInput(start_date="2025-01-01", end_date="2025-06-01")

    def test_at_90_days(self):
        result = DateRangeInput(start_date="2025-01-01", end_date="2025-04-01")
        assert result.start_date.isoformat() == "2025-01-01"

    def test_bad_date_string(self):
        with pytest.raises((ValidationError, ValueError)):
            DateRangeInput(start_date="not-a-date", end_date="2025-01-01")


class TestCreateWorkoutInput:
    """Tests for CreateWorkoutInput validation."""

    def test_valid(self):
        result = CreateWorkoutInput(
            date="2025-06-01",
            sport="Run",
            title="Morning Run",
            duration_minutes=60,
        )
        assert result.sport == "Run"
        assert result.duration_minutes == 60
        assert result.is_hidden is None

    def test_accepts_hidden_flag(self):
        result = CreateWorkoutInput(
            date="2025-06-01",
            sport="Run",
            title="Morning Run",
            duration_minutes=60,
            is_hidden=True,
        )
        assert result.is_hidden is True

    def test_valid_datetime(self):
        result = CreateWorkoutInput(
            date="2025-06-01T09:30:00",
            sport="Run",
            title="Morning Run",
            duration_minutes=60,
        )
        assert result.date.isoformat() == "2025-06-01T09:30:00"

    def test_empty_title(self):
        with pytest.raises(ValidationError):
            CreateWorkoutInput(date="2025-06-01", sport="Run", title="", duration_minutes=60)

    def test_title_too_long(self):
        with pytest.raises(ValidationError):
            CreateWorkoutInput(date="2025-06-01", sport="Run", title="x" * 201, duration_minutes=60)

    def test_duration_zero(self):
        with pytest.raises(ValidationError):
            CreateWorkoutInput(date="2025-06-01", sport="Run", title="Test", duration_minutes=0)

    def test_duration_too_large(self):
        with pytest.raises(ValidationError):
            CreateWorkoutInput(date="2025-06-01", sport="Run", title="Test", duration_minutes=1441)

    def test_bad_sport(self):
        with pytest.raises(ValidationError):
            CreateWorkoutInput(date="2025-06-01", sport="Hockey", title="Test", duration_minutes=60)

    def test_description_long_is_allowed(self):
        # FORK: create used to cap description at 2000 chars; update never did.
        # TP itself has no such cap, so the create-side cap was removed.
        params = CreateWorkoutInput(
                date="2025-06-01",
                sport="Run",
                title="Test",
                duration_minutes=60,
                description="x" * 5000,
            )
        assert len(params.description) == 5000

    def test_other_without_duration_is_allowed(self):
        # FORK: Other ＝行程標記課（移動日等），TP 接受時長空白，不必先建 1 分鐘再改 0。
        params = CreateWorkoutInput(date="2025-06-01", sport="Other", title="移動日", description="x")
        assert params.duration_minutes is None

    def test_non_other_without_duration_still_rejected(self):
        for sport in ("Run", "Bike", "Swim", "DayOff"):
            with pytest.raises(ValidationError):
                CreateWorkoutInput(date="2025-06-01", sport=sport, title="Test")

    def test_duration_float_is_allowed(self):
        # FORK: create used to require int; update accepts float. Aligned to float.
        params = CreateWorkoutInput(date="2025-06-01", sport="Run", title="Test", duration_minutes=92.5)
        assert params.duration_minutes == 92.5

    def test_distance_km_valid(self):
        result = CreateWorkoutInput(
            date="2025-06-01",
            sport="Bike",
            title="Ride",
            duration_minutes=120,
            distance_km=42.5,
        )
        assert result.distance_km == 42.5

    def test_distance_km_negative(self):
        with pytest.raises(ValidationError):
            CreateWorkoutInput(
                date="2025-06-01",
                sport="Bike",
                title="Ride",
                duration_minutes=60,
                distance_km=-1,
            )

    def test_distance_km_too_large(self):
        with pytest.raises(ValidationError):
            CreateWorkoutInput(
                date="2025-06-01",
                sport="Bike",
                title="Ride",
                duration_minutes=60,
                distance_km=1001,
            )

    def test_tss_planned_valid(self):
        result = CreateWorkoutInput(
            date="2025-06-01",
            sport="Run",
            title="Long Run",
            duration_minutes=90,
            tss_planned=150.5,
        )
        assert result.tss_planned == 150.5

    def test_tss_planned_negative(self):
        with pytest.raises(ValidationError):
            CreateWorkoutInput(
                date="2025-06-01",
                sport="Run",
                title="Run",
                duration_minutes=60,
                tss_planned=-10,
            )


class TestPeaksInput:
    """Tests for PeaksInput validation."""

    def test_valid_bike(self):
        result = PeaksInput(sport="Bike", pr_type="power20min")
        assert result.sport == "Bike"

    def test_valid_run(self):
        result = PeaksInput(sport="Run", pr_type="speed5K")
        assert result.sport == "Run"

    def test_invalid_pr_type(self):
        with pytest.raises(ValidationError, match="invalid_type"):
            PeaksInput(sport="Bike", pr_type="invalid_type")


class TestUpdateWorkoutInput:
    """Tests for UpdateWorkoutInput validation."""

    def test_accepts_date_only(self):
        result = UpdateWorkoutInput(workout_id="123", date="2026-04-14")
        assert result.date is not None
        assert result.date.isoformat() == "2026-04-14"
        assert result.is_hidden is None

    def test_accepts_hidden_flag(self):
        result = UpdateWorkoutInput(workout_id="123", is_hidden=True)
        assert result.is_hidden is True

    def test_accepts_datetime(self):
        result = UpdateWorkoutInput(workout_id="123", date="2026-04-14T16:45:00")
        assert result.date is not None
        assert result.date.isoformat() == "2026-04-14T16:45:00"

    def test_rejects_invalid_datetime(self):
        with pytest.raises(ValidationError):
            UpdateWorkoutInput(workout_id="123", date="2026-04-14 99:45:00")


class TestFitnessInput:
    """Tests for FitnessInput validation."""

    def test_days_only(self):
        result = FitnessInput(days=30)
        assert result.days == 30
        assert result.start_date is None

    def test_date_range(self):
        result = FitnessInput(start_date="2025-01-01", end_date="2025-03-01")
        assert result.start_date is not None
        assert result.end_date is not None

    def test_start_without_end(self):
        with pytest.raises(ValidationError, match="both"):
            FitnessInput(start_date="2025-01-01")

    def test_inverted_dates(self):
        with pytest.raises(ValidationError, match="before"):
            FitnessInput(start_date="2025-03-01", end_date="2025-01-01")

    def test_days_zero(self):
        with pytest.raises(ValidationError):
            FitnessInput(days=0)

    def test_days_too_large(self):
        with pytest.raises(ValidationError):
            FitnessInput(days=400)


class TestFormatValidationError:
    """Tests for format_validation_error helper."""

    def test_output_format(self):
        try:
            WorkoutIdInput(workout_id="abc")
        except ValidationError as e:
            msg = format_validation_error(e)
            assert "workout_id" in msg
