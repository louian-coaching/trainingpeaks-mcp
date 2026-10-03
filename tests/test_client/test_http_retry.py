"""FORK 2026-10-03: transient-failure retry policy in TPClient._request."""

import httpx
import pytest

from tp_mcp.client.http import APIResponse, ErrorCode, TPClient


def _client(handler, monkeypatch):
    monkeypatch.setenv("TP_MCP_RETRY_BASE_S", "0")
    c = TPClient()
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def ok_token():
        return APIResponse(success=True)

    async def no_throttle():
        return None

    monkeypatch.setattr(c, "_ensure_access_token", ok_token)
    monkeypatch.setattr(c, "_throttle", no_throttle)
    return c


class Seq:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        step = self.steps.pop(0) if self.steps else self.last
        self.last = step
        if isinstance(step, Exception):
            raise step
        return step


@pytest.mark.asyncio
async def test_get_retries_timeout_then_succeeds(monkeypatch):
    seq = Seq(httpx.ReadTimeout("slow"), httpx.Response(200, json={"a": 1}))
    r = await _client(seq, monkeypatch)._request("GET", "/x")
    assert r.success and r.data == {"a": 1} and seq.calls == 2


@pytest.mark.asyncio
async def test_get_retries_503_and_gives_up(monkeypatch):
    seq = Seq(httpx.Response(503, text="busy"))
    r = await _client(seq, monkeypatch)._request("GET", "/x")
    assert not r.success and seq.calls == 3
    assert "gave up after 3 attempts" in r.message


@pytest.mark.asyncio
async def test_post_read_timeout_is_outcome_unknown_and_not_retried(monkeypatch):
    seq = Seq(httpx.ReadTimeout("slow"), httpx.Response(200, json={}))
    r = await _client(seq, monkeypatch)._request("POST", "/w", json={"t": 1})
    assert r.error_code == ErrorCode.WRITE_OUTCOME_UNKNOWN and seq.calls == 1
    assert "MAY have landed" in r.message


@pytest.mark.asyncio
async def test_post_connect_error_is_retried(monkeypatch):
    seq = Seq(httpx.ConnectError("refused"), httpx.Response(200, json={"id": 5}))
    r = await _client(seq, monkeypatch)._request("POST", "/w", json={"t": 1})
    assert r.success and r.data == {"id": 5} and seq.calls == 2


@pytest.mark.asyncio
async def test_put_429_is_retried_but_503_is_not(monkeypatch):
    seq = Seq(httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json={}))
    r = await _client(seq, monkeypatch)._request("PUT", "/w", json={})
    assert r.success and seq.calls == 2
    seq2 = Seq(httpx.Response(503, text="busy"), httpx.Response(200, json={}))
    r2 = await _client(seq2, monkeypatch)._request("PUT", "/w", json={})
    assert not r2.success and seq2.calls == 1


@pytest.mark.asyncio
async def test_retries_can_be_disabled(monkeypatch):
    monkeypatch.setenv("TP_MCP_RETRIES", "0")
    seq = Seq(httpx.ReadTimeout("slow"), httpx.Response(200, json={}))
    r = await _client(seq, monkeypatch)._request("GET", "/x")
    assert not r.success and seq.calls == 1 and r.error_code == ErrorCode.NETWORK_ERROR


@pytest.mark.asyncio
async def test_get_stops_after_two_timeouts(monkeypatch):
    seq = Seq(httpx.ReadTimeout("slow"))
    r = await _client(seq, monkeypatch)._request("GET", "/x")
    assert not r.success and seq.calls == 2 and "gave up after 2 attempts" in r.message
