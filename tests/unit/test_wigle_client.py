"""Tests for the WiGLE client. HTTP mocked via respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from warroute.clients.wigle import (
    SEARCH_PATH,
    WIGLE_API_BASE,
    BBox,
    WigleAuthError,
    WigleClient,
    WigleError,
    WigleRateLimitError,
)


def _bbox() -> BBox:
    return BBox(south=44.93, north=44.95, west=-72.21, east=-72.18)


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the 1 req/sec sleep in unit tests."""
    monkeypatch.setattr("warroute.clients.wigle.MIN_INTERVAL_SEC", 0.0)


@respx.mock
async def test_search_bbox_parses_results() -> None:
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "totalResults": 17,
                "results": [{"netid": "AA:BB:CC:DD:EE:FF", "ssid": "TestNet"}],
            },
        )
    )
    async with WigleClient() as wigle:
        result = await wigle.search_bbox(_bbox())
    assert result.total_results == 17
    assert result.networks[0]["ssid"] == "TestNet"


@respx.mock
async def test_search_bbox_raises_on_401() -> None:
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(return_value=httpx.Response(401))
    async with WigleClient() as wigle:
        with pytest.raises(WigleAuthError):
            await wigle.search_bbox(_bbox())


@respx.mock
async def test_search_bbox_raises_on_429() -> None:
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(return_value=httpx.Response(429))
    async with WigleClient() as wigle:
        with pytest.raises(WigleRateLimitError):
            await wigle.search_bbox(_bbox())


@respx.mock
async def test_search_bbox_raises_on_success_false() -> None:
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(
            200, json={"success": False, "message": "quota exceeded"}
        )
    )
    async with WigleClient() as wigle:
        with pytest.raises(WigleError, match="success=false"):
            await wigle.search_bbox(_bbox())


@respx.mock
async def test_search_bbox_sends_bbox_params() -> None:
    route = respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(200, json={"success": True, "totalResults": 0, "results": []})
    )
    async with WigleClient() as wigle:
        await wigle.search_bbox(_bbox())
    assert route.called
    sent_params = dict(route.calls.last.request.url.params)
    assert sent_params["latrange1"] == "44.930000"
    assert sent_params["latrange2"] == "44.950000"
    assert sent_params["longrange1"] == "-72.210000"
    assert sent_params["longrange2"] == "-72.180000"


@respx.mock
async def test_only_mine_flag_propagates() -> None:
    route = respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(200, json={"success": True, "totalResults": 0, "results": []})
    )
    async with WigleClient() as wigle:
        await wigle.search_bbox(_bbox(), only_mine=True)
    assert dict(route.calls.last.request.url.params)["onlymine"] == "true"


def test_missing_credentials_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from warroute.config import get_settings

    monkeypatch.setenv("WIGLE_NAME", "")
    monkeypatch.setenv("WIGLE_TOKEN", "")
    get_settings.cache_clear()
    with pytest.raises(WigleAuthError):
        WigleClient()
