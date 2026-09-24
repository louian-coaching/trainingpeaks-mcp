"""Tests for health metrics tools."""

from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.client.http import APIResponse
from tp_mcp.tools.metrics import tp_get_metrics, tp_get_nutrition, tp_log_metrics


class TestLogMetrics:
    @pytest.mark.asyncio
    async def test_log_single_metric(self):
        """Log weight only."""
        response = APIResponse(success=True, data=None)
        with patch("tp_mcp.tools.metrics.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_log_metrics(date="2026-03-01", weight_kg=75.5)

        assert result["success"] is True
        assert "weight_kg" in result["logged"]
        payload = mock_instance.post.call_args[1]["json"]
        assert payload["athleteId"] == 123
        assert len(payload["details"]) == 1
        assert payload["details"][0]["type"] == 9  # weight type ID
        assert payload["details"][0]["value"] == 75.5

    @pytest.mark.asyncio
    async def test_log_multiple_metrics(self):
        """Log multiple metrics in one request."""
        response = APIResponse(success=True, data=None)
        with patch("tp_mcp.tools.metrics.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_log_metrics(
                date="2026-03-01", weight_kg=75.5, hrv=45.0, sleep_hours=7.5,
            )

        assert result["success"] is True
        assert len(result["logged"]) == 3
        payload = mock_instance.post.call_args[1]["json"]
        assert len(payload["details"]) == 3

    @pytest.mark.asyncio
    async def test_log_no_metrics_rejected(self):
        result = await tp_log_metrics(date="2026-03-01")
        assert result["isError"] is True

    @pytest.mark.asyncio
    async def test_log_invalid_injury(self):
        """Injury value 0 should be rejected (min is 1)."""
        result = await tp_log_metrics(date="2026-03-01", injury=0)
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_metric_type_ids(self):
        """Verify metric type IDs match expected values."""
        from tp_mcp.tools.metrics import METRIC_TYPES

        assert METRIC_TYPES["pulse"]["type"] == 5
        assert METRIC_TYPES["weight_kg"]["type"] == 9
        assert METRIC_TYPES["sleep_hours"]["type"] == 6
        assert METRIC_TYPES["hrv"]["type"] == 60
        assert METRIC_TYPES["spo2"]["type"] == 53
        assert METRIC_TYPES["steps"]["type"] == 58


class TestGetMetrics:
    @pytest.mark.asyncio
    async def test_success(self):
        data = [{"date": "2026-03-01", "details": []}]
        response = APIResponse(success=True, data=data)
        with patch("tp_mcp.tools.metrics.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_get_metrics("2026-03-01", "2026-03-07")

        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_empty_range(self):
        response = APIResponse(success=True, data=[])
        with patch("tp_mcp.tools.metrics.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_get_metrics("2026-03-01", "2026-03-07")

        assert "No metrics" in result.get("message", "")


class TestGetNutrition:
    @pytest.mark.asyncio
    async def test_success(self):
        response = APIResponse(success=True, data=[{"date": "2026-03-01"}])
        with patch("tp_mcp.tools.metrics.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_get_nutrition("2026-03-01", "2026-03-07")

        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_empty(self):
        response = APIResponse(success=True, data=[])
        with patch("tp_mcp.tools.metrics.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_get_nutrition("2026-03-01", "2026-03-07")

        assert "No nutrition" in result.get("message", "")


def test_daily_table_compact():
    from tp_mcp.tools.metrics import _daily_table
    rows = _daily_table([{"timeStamp": "2026-09-24T00:00:00", "details": [
        {"label": "HRV", "value": 98}, {"label": "Sleep Hours", "value": 6.5333},
        {"label": "Body Battery", "value": [5, 57, 34.5103]}, {"label": "Time Awake", "value": 0.1}]}])
    assert rows == [{"date": "2026-09-24", "hrv": 98, "sleep_h": 6.53, "body_battery": [5, 57, 34.5]}]
