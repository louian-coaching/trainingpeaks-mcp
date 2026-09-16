"""Tests for the fork-only hardening changes (羅教練 fork, 2026-09).

Each test here guards one behaviour that upstream did not have and that
previously produced "success: true but nothing landed" incidents
(see tp-ai-layer/tools/PITFALLS.md TECH-35 family).
"""

import json

import httpx
import pytest

from tp_mcp.client.http import ErrorCode, TPClient


# ---------------------------------------------------------------------------
# 1. Unknown argument keys are rejected instead of silently dropped
# ---------------------------------------------------------------------------


class TestUnknownArgsRejected:
    @pytest.mark.asyncio
    async def test_unknown_key_returns_invalid_args(self):
        from tp_mcp.server import _TOOL_HANDLERS, call_tool

        called = {"n": 0}

        async def spy(args):
            called["n"] += 1
            return {"status": "ok"}

        original = _TOOL_HANDLERS.get("tp_update_workout")
        _TOOL_HANDLERS["tp_update_workout"] = spy
        try:
            # `tss` is the classic typo for `tss_planned` (TECH-35)
            out = await call_tool("tp_update_workout", {"workout_id": 1, "tss": 70})
        finally:
            _TOOL_HANDLERS["tp_update_workout"] = original

        payload = json.loads(out[0].text)
        assert payload["isError"] is True
        assert payload["error_code"] == "INVALID_ARGS"
        assert "tss" in payload["message"]
        assert "tss_planned" in payload["message"]  # allowed list is echoed back
        assert called["n"] == 0  # handler never ran

    @pytest.mark.asyncio
    async def test_athlete_and_save_to_are_not_unknown(self):
        from tp_mcp.server import _TOOL_HANDLERS, call_tool

        captured = {}

        async def spy(args):
            captured.update(args)
            return {"status": "ok"}

        original = _TOOL_HANDLERS.get("tp_auth_status")
        _TOOL_HANDLERS["tp_auth_status"] = spy
        try:
            out = await call_tool("tp_auth_status", {"athlete": "Someone"})
        finally:
            _TOOL_HANDLERS["tp_auth_status"] = original

        payload = json.loads(out[0].text)
        assert payload.get("isError") is not True
        assert captured == {}

    def test_every_handler_only_reads_schema_keys(self):
        """Static guard: a handler reading an arg key that is not in its schema
        would now be unreachable. Fails loudly if upstream adds such a key."""
        import inspect
        import re

        from tp_mcp import server

        by_name = {t.name: t for t in server.TOOLS}
        offenders = {}
        for name, handler in server._TOOL_HANDLERS.items():
            props = set(by_name[name].input_schema.get("properties", {}))
            src = inspect.getsource(handler)
            keys = set(re.findall(r'args(?:\.get\(|\[)"(\w+)"', src))
            keys |= set(re.findall(r'args\.pop\("(\w+)"', src))
            extra = keys - props - {"athlete", "save_to"}
            if extra:
                offenders[name] = sorted(extra)
        assert offenders == {}


# ---------------------------------------------------------------------------
# 2/3. HTTP response handling
# ---------------------------------------------------------------------------


class TestResponseHandling:
    def test_200_non_json_body_is_failure(self):
        client = TPClient()
        html = "<html><body>Please log in</body></html>"
        response = httpx.Response(
            status_code=200, content=html.encode(), headers={"content-type": "text/html"}
        )
        result = client._handle_response(response)
        assert result.success is False
        assert result.error_code == ErrorCode.UNPARSEABLE_RESPONSE
        assert "log in" in result.message

    def test_201_non_json_body_is_failure(self):
        client = TPClient()
        response = httpx.Response(status_code=201, content=b"Just a moment...")
        result = client._handle_response(response)
        assert result.success is False
        assert result.error_code == ErrorCode.UNPARSEABLE_RESPONSE

    def test_200_empty_body_is_still_success(self):
        client = TPClient()
        response = httpx.Response(status_code=200, content=b"")
        result = client._handle_response(response)
        assert result.success is True
        assert result.data is None

    def test_200_json_is_success(self):
        client = TPClient()
        response = httpx.Response(status_code=200, json={"workoutId": 1})
        result = client._handle_response(response)
        assert result.success is True
        assert result.data == {"workoutId": 1}

    def test_generic_error_carries_body(self):
        client = TPClient()
        body = '{"Message":"tssPlanned must be a number"}'
        response = httpx.Response(status_code=400, content=body.encode())
        result = client._handle_response(response)
        assert result.success is False
        assert result.error_code == ErrorCode.API_ERROR
        assert "tssPlanned must be a number" in result.message

    def test_generic_error_body_is_truncated(self):
        client = TPClient()
        response = httpx.Response(status_code=500, content=b"x" * 5000)
        result = client._handle_response(response)
        assert len(result.message) < 400
