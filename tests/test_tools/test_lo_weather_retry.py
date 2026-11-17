"""lo_weather._fetch — retry on 429 / 5xx / timeout (2026/11/17 週檢: parallel analyses all failed)."""

from __future__ import annotations

import httpx
import pytest

from tp_mcp.tools import lo_weather


class _Client:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def get(self, url):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return httpx.Response(r, json={"ok": True}, request=httpx.Request("GET", url))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def nap(_):
        return None
    monkeypatch.setattr(lo_weather.asyncio, "sleep", nap)


async def test_429_then_timeout_then_ok():
    c = _Client([429, httpx.ReadTimeout("slow"), 200])
    assert await lo_weather._fetch(c, "https://x/y") == {"ok": True}
    assert c.calls == 3


async def test_gives_up_after_three_tries_and_keeps_the_reason():
    c = _Client([503, 503, 503])
    with pytest.raises(httpx.HTTPStatusError):
        await lo_weather._fetch(c, "https://x/y")
    assert c.calls == 3


async def test_a_400_is_not_retried():
    c = _Client([400])
    with pytest.raises(httpx.HTTPStatusError):
        await lo_weather._fetch(c, "https://x/y")
    assert c.calls == 1
